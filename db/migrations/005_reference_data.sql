-- 005_reference_data.sql — permissions and roles.
--
-- Permissions are fine-grained and named after what they let you do, not after
-- who normally holds them. Roles are the bundles. Idempotent so it can be
-- re-run safely while the role model is still settling.

INSERT INTO app.permission (code, description) VALUES
    -- Camera registry
    ('camera.read_cross_department', 'See cameras owned by other departments within your jurisdiction'),
    ('camera.create',               'Onboard new cameras (manual, bulk or API)'),
    ('camera.update',               'Modify camera metadata'),
    ('camera.delete',               'Remove a camera from the registry'),
    ('camera.export',               'Export camera lists to CSV or GeoJSON'),
    ('camera.credential_use',       'Decrypt and use stored camera credentials'),
    ('camera.credential_rotate',    'Store or rotate camera credentials'),
    ('camera.health_write',         'Record camera health probe results'),
    -- Reporting
    ('report.gap_analysis',         'Run coverage and blind-spot analysis'),
    ('report.ageing',               'Run ageing-infrastructure and replacement reports'),
    -- Oversight
    ('audit.read',                  'Read the full audit trail, not just your own actions'),
    ('audit.verify',                'Run audit chain integrity verification'),
    -- Administration
    ('user.manage',                 'Create and modify user accounts'),
    ('role.manage',                 'Assign roles and permissions'),
    ('apikey.manage',               'Issue and revoke machine API keys'),
    ('jurisdiction.manage',         'Modify the jurisdiction hierarchy'),
    ('department.manage',           'Modify departments'),
    -- Reserved for Models 2 and 3, defined now so the role model is stable
    ('stream.view',                 'View live or recorded video through the unified viewer'),
    ('stream.view_restricted',      'View cameras flagged as sensitive'),
    ('watchlist.read',              'Search watchlist entities'),
    ('watchlist.write',             'Add or modify watchlist entities'),
    ('alert.read',                  'View generated alerts'),
    ('alert.acknowledge',           'Acknowledge and triage alerts'),
    ('alert.close',                 'Resolve or mark alerts as false positives'),
    ('evidence.export',             'Produce signed evidence packages'),
    ('federation.manage',           'Configure VMS adapters and federation connectors')
ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description;


INSERT INTO app.role (code, name, description, is_statewide) VALUES
    ('state_admin',      'State Administrator',
     'Full platform administration across all districts and departments.', true),
    ('state_viewer',     'State Control Room Operator',
     'Statewide read access to the registry, map, health and alerts.', true),
    ('auditor',          'Auditor',
     'Independent oversight. Reads the full audit trail statewide but cannot modify assets.', true),
    ('district_admin',   'District Administrator',
     'Administers cameras and users within one district subtree, across departments.', false),
    ('district_operator','District Operator',
     'Control-room operator scoped to one district subtree, across departments.', false),
    ('department_operator', 'Department Operator',
     'Scoped to one department within one jurisdiction subtree. The default role.', false),
    ('field_technician', 'Field Technician',
     'Updates camera metadata and records health checks. No viewing rights.', false),
    ('integrator',       'System Integrator (machine)',
     'Machine role for API-based camera onboarding. Held by API keys, not people.', false)
ON CONFLICT (code) DO UPDATE
    SET name = EXCLUDED.name,
        description = EXCLUDED.description,
        is_statewide = EXCLUDED.is_statewide;


-- Role -> permission bundles.
WITH grants(role_code, permission_code) AS (VALUES
    -- State administrator: everything defined above.
    ('state_admin', 'camera.read_cross_department'),
    ('state_admin', 'camera.create'), ('state_admin', 'camera.update'),
    ('state_admin', 'camera.delete'), ('state_admin', 'camera.export'),
    ('state_admin', 'camera.credential_rotate'), ('state_admin', 'camera.credential_use'),
    ('state_admin', 'camera.health_write'),
    ('state_admin', 'report.gap_analysis'), ('state_admin', 'report.ageing'),
    ('state_admin', 'audit.read'), ('state_admin', 'audit.verify'),
    ('state_admin', 'user.manage'), ('state_admin', 'role.manage'),
    ('state_admin', 'apikey.manage'), ('state_admin', 'jurisdiction.manage'),
    ('state_admin', 'department.manage'),
    ('state_admin', 'stream.view'), ('state_admin', 'stream.view_restricted'),
    ('state_admin', 'watchlist.read'), ('state_admin', 'watchlist.write'),
    ('state_admin', 'alert.read'), ('state_admin', 'alert.acknowledge'),
    ('state_admin', 'alert.close'), ('state_admin', 'evidence.export'),
    ('state_admin', 'federation.manage'),

    -- State control room: sees everything, changes nothing structural.
    ('state_viewer', 'camera.read_cross_department'), ('state_viewer', 'camera.export'),
    ('state_viewer', 'report.gap_analysis'), ('state_viewer', 'report.ageing'),
    ('state_viewer', 'stream.view'),
    ('state_viewer', 'watchlist.read'),
    ('state_viewer', 'alert.read'), ('state_viewer', 'alert.acknowledge'),

    -- Auditor: oversight only. Deliberately has no camera.update and no
    -- stream.view — an auditor should not need to watch video to audit access.
    ('auditor', 'camera.read_cross_department'),
    ('auditor', 'audit.read'), ('auditor', 'audit.verify'),
    ('auditor', 'camera.export'),

    -- District administrator: full asset management inside their subtree.
    ('district_admin', 'camera.read_cross_department'),
    ('district_admin', 'camera.create'), ('district_admin', 'camera.update'),
    ('district_admin', 'camera.delete'), ('district_admin', 'camera.export'),
    ('district_admin', 'camera.credential_rotate'),
    ('district_admin', 'report.gap_analysis'), ('district_admin', 'report.ageing'),
    ('district_admin', 'user.manage'),
    ('district_admin', 'stream.view'),
    ('district_admin', 'watchlist.read'), ('district_admin', 'watchlist.write'),
    ('district_admin', 'alert.read'), ('district_admin', 'alert.acknowledge'),
    ('district_admin', 'alert.close'),

    -- District operator: cross-department visibility, no structural change.
    ('district_operator', 'camera.read_cross_department'),
    ('district_operator', 'camera.export'),
    ('district_operator', 'report.gap_analysis'),
    ('district_operator', 'stream.view'),
    ('district_operator', 'watchlist.read'),
    ('district_operator', 'alert.read'), ('district_operator', 'alert.acknowledge'),

    -- Department operator: no cross-department permission, so RLS confines them
    -- to their own department's cameras. This is the interesting demo case.
    ('department_operator', 'camera.export'),
    ('department_operator', 'stream.view'),
    ('department_operator', 'alert.read'),

    -- Field technician: maintenance only.
    ('field_technician', 'camera.update'),
    ('field_technician', 'camera.health_write'),
    ('field_technician', 'camera.credential_use'),
    ('field_technician', 'report.ageing'),

    -- Integrator (machine): onboarding only. Cannot read video or alerts.
    ('integrator', 'camera.create'),
    ('integrator', 'camera.update'),
    ('integrator', 'camera.health_write')
)
INSERT INTO app.role_permission (role_id, permission_code)
SELECT r.id, g.permission_code
FROM   grants g
JOIN   app.role r ON r.code = g.role_code
ON CONFLICT (role_id, permission_code) DO NOTHING;
