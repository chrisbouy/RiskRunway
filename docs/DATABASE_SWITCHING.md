# Database Switching Feature

## Overview

RiskRunway Mapper supports multiple databases that can be switched on-the-fly from the Kanban board. This allows you to:
- Keep production data separate from test scenarios
- Create isolated use case demonstrations
- Run automated tests without affecting production data

## Available Databases

### 1. **Production DB** (Default)
- **Path**: `data/ipfs_mapper.db`
- **Purpose**: Main production database with real submissions and quotes
- **Default**: Yes

### 2. **Use Cases DB**
- **Path**: `data/use_cases.db`
- **Purpose**: Test scenarios and demonstrations
- **Seeded with**: 5 sample test scenarios

### 3. **Test DB**
- **Path**: `/tmp/ipfs_mapper_test.db`
- **Purpose**: Automated testing (E2E tests)
- **Note**: Temporary location, may be cleared on system restart

## How to Switch Databases

### From the UI (Kanban Board)

1. Log in to the application
2. Look for the **database dropdown** at the top of the left sidebar
3. Select the database you want to use:
   - Production DB
   - Use Cases DB
   - Test DB
4. The board will automatically reload with data from the selected database

**Note**: Your database selection is saved in your session and will persist until you log out.

### From the Command Line

Use the `db_manager.py` utility:

```bash
# List all available databases
python utils/db_manager.py list

# Initialize a database (create tables)
python utils/db_manager.py init use_cases

# Seed use_cases database with test scenarios
python utils/db_manager.py seed use_cases

# Clear and reinitialize a database
python utils/db_manager.py clear test
```

## Use Cases Database

The use_cases database comes pre-seeded with 5 test scenarios:

1. **Test Scenario 1: Simple GL Quote**
   - Status: In Progress
   - State: CA
   - Effective: 30 days from now

2. **Test Scenario 2: Multi-Coverage Package**
   - Status: Chosen
   - State: TX
   - Effective: 45 days from now

3. **Test Scenario 3: Workers Comp Renewal**
   - Status: Sent to Finance
   - State: NY
   - Effective: 60 days from now

4. **Test Scenario 4: Cyber Liability**
   - Status: Received
   - State: FL
   - Effective: 15 days from now

5. **Test Scenario 5: Commercial Auto**
   - Status: In Progress
   - State: IL
   - Effective: 90 days from now

### Test User Credentials

When using the use_cases database, you can log in with:
- **Username**: `test_user`
- **Password**: `test123`

## Configuration

Database paths are configured in `config.py`:

```python
DATABASES = {
    'production': 'data/ipfs_mapper.db',
    'use_cases': 'data/use_cases.db',
    'test': '/tmp/ipfs_mapper_test.db'
}
```

You can override these paths using environment variables:
- `DATABASE_PATH` - Override production database path
- `USE_CASE_DB_PATH` - Override use cases database path

## API Endpoints

### Get Current Database
```
GET /api/database/current
```

Response:
```json
{
  "success": true,
  "current_database": "production",
  "available_databases": ["production", "use_cases", "test"]
}
```

### Switch Database
```
POST /api/database/switch
Content-Type: application/json

{
  "database": "use_cases"
}
```

Response:
```json
{
  "success": true,
  "current_database": "use_cases",
  "message": "Switched to use_cases database"
}
```

## Technical Details

### Database Instances
- Each database has its own SQLAlchemy engine and session factory
- Database instances are cached to avoid reconnection overhead
- Switching databases updates the global database instance

### Session Persistence
- The selected database is stored in the Flask session
- Database selection persists across page refreshes
- Logging out clears the database selection (reverts to production)

### Automatic Initialization
- When switching to a database, tables are automatically created if they don't exist
- This ensures all databases have the correct schema

## Best Practices

1. **Use Production for Real Work**: Keep production database for actual submissions and quotes
2. **Use Cases for Demos**: Use the use_cases database for demonstrations and training
3. **Test for Automation**: Use the test database for automated E2E tests
4. **Clear Test Data**: Regularly clear the test database to avoid clutter
5. **Backup Production**: Always backup production database before major changes

## Troubleshooting

### Database Not Switching
- Check browser console for errors
- Verify you're logged in
- Try refreshing the page

### Missing Tables
- Run: `python utils/db_manager.py init <db_name>`
- Tables are auto-created on first switch, but manual init can help

### Lost Data After Restart
- Test database (`/tmp/ipfs_mapper_test.db`) may be cleared on system restart
- This is intentional - use production or use_cases for persistent data

### Permission Errors
- Ensure the data directory is writable
- Check file permissions on database files

## Future Enhancements

Potential improvements:
- Add more seed scenarios to use_cases database
- Support for custom database names
- Database export/import functionality
- Database comparison tools
- Automatic backup before switching

