-- deploy/seed.example.sql — AD 组 → 平台角色映射示例（按贵司实际组名修改）
INSERT INTO role_mapping (ad_group, role) VALUES
('CN=SMS-Admins,OU=Security,DC=xtc,DC=com',    'admin'),
('CN=SMS-Approvers,OU=Security,DC=xtc,DC=com', 'approver'),
('CN=SMS-Operators,OU=Security,DC=xtc,DC=com', 'operator'),
('CN=SMS-Viewers,OU=Security,DC=xtc,DC=com',   'viewer')
ON CONFLICT (ad_group) DO NOTHING;
