import json, subprocess

# Get current task def
result = subprocess.run(['aws', 'ecs', 'describe-task-definition', '--task-definition', 'riskrunway', '--region', 'us-east-1', '--output', 'json'], capture_output=True, text=True)
td = json.loads(result.stdout)['taskDefinition']

container = td['containerDefinitions'][0]

# Update TENANT_DATABASE_MAP - replace paulin with classic
new_map = json.dumps({
    "default": "postgresql://riskrunway:RiskRunway2026!@riskrunway-db.cu54eyu4cy2j.us-east-1.rds.amazonaws.com:5432/riskrunway",
    "classic": "postgresql://riskrunway:RiskRunway2026!@riskrunway-db.cu54eyu4cy2j.us-east-1.rds.amazonaws.com:5432/riskrunway_classic"
})

for env in container['environment']:
    if env['name'] == 'TENANT_DATABASE_MAP':
        env['value'] = new_map
        break

# Build the input for register-task-definition
reg_input = {
    'family': td['family'],
    'taskRoleArn': td.get('taskRoleArn', ''),
    'executionRoleArn': td.get('executionRoleArn', ''),
    'networkMode': td['networkMode'],
    'containerDefinitions': td['containerDefinitions'],
    'requiresCompatibilities': td.get('requiresCompatibilities', []),
    'cpu': td.get('cpu', ''),
    'memory': td.get('memory', ''),
}

output_path = 'scripts/task_def_classic.json'
with open(output_path, 'w') as f:
    json.dump(reg_input, f)

print(f'Task def written to {output_path}')
print(f'New TENANT_DATABASE_MAP: {new_map}')
