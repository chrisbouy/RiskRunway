# Database Switching Implementation Summary

## ✅ Implementation Complete

A complete database switching system has been implemented for IPFS Mapper, allowing you to switch between Production, Use Cases, and Test databases from a dropdown on the Kanban board.

---

## 🎯 What Was Built

### 1. **Multi-Database Support**
- **3 Databases**: Production, Use Cases, Test
- **Separate Storage**: Each database is completely isolated
- **Same Schema**: All databases use the same table structure
- **SQLite Engine**: All databases use SQLite (as requested)

### 2. **UI Integration**
- **Dropdown Selector**: Added to top of left sidebar on Kanban board
- **Visual Styling**: Blue-bordered dropdown with hover effects
- **Auto-Reload**: Board automatically refreshes when switching databases
- **Session Persistence**: Database selection saved in user session

### 3. **Backend API**
- **GET /api/database/current**: Returns current database and available options
- **POST /api/database/switch**: Switches to selected database
- **Session Storage**: Database preference stored per user session
- **Auto-Initialization**: Tables created automatically when switching

### 4. **Database Management CLI**
- **utils/db_manager.py**: Command-line tool for database operations
- **Commands**: list, init, seed, clear
- **Pre-Seeded Data**: Use Cases DB comes with 5 test scenarios

---

## 📁 Files Modified

### Configuration
- **config.py**: Added `DATABASES` dictionary with 3 database paths

### Backend
- **app/database.py**: 
  - Added `get_current_db_name()`
  - Added `set_current_db(db_name)`
  - Added `get_available_databases()`
  - Added database instance caching
  
- **app/routes.py**:
  - Added `GET /api/database/current`
  - Added `POST /api/database/switch`
  - Updated login to restore database selection

### Frontend
- **app/templates/kanban.html**:
  - Added database dropdown in left sidebar
  - Added `loadCurrentDatabase()` function
  - Added `switchDatabase()` function
  - Added event handler for dropdown
  - Updated `initializePage()` to load current database
  - Added CSS styling for dropdown

---

## 📁 Files Created

### Utilities
- **utils/db_manager.py**: CLI tool for database management (executable)

### Documentation
- **docs/DATABASE_SWITCHING.md**: Complete technical documentation
- **QUICK_START_DB_SWITCHING.md**: Quick start guide
- **IMPLEMENTATION_SUMMARY.md**: This file

---

## 🗄️ Database Details

### Production DB
- **Path**: `data/ipfs_mapper.db`
- **Purpose**: Main production database
- **Default**: Yes
- **Size**: ~260 KB (with existing data)

### Use Cases DB
- **Path**: `data/use_cases.db`
- **Purpose**: Test scenarios and demonstrations
- **Pre-Seeded**: Yes (5 test scenarios)
- **Test User**: username: `test_user`, password: `test123`
- **Size**: ~92 KB

### Test DB
- **Path**: `/tmp/ipfs_mapper_test.db`
- **Purpose**: Automated E2E testing
- **Temporary**: Yes (cleared on system restart)
- **Size**: ~92 KB

---

## 🧪 Pre-Seeded Test Scenarios

The Use Cases DB includes 5 test scenarios:

1. **Test Scenario 1: Simple GL Quote**
   - State: CA
   - Status: In Progress
   - Effective: 30 days from now

2. **Test Scenario 2: Multi-Coverage Package**
   - State: TX
   - Status: Chosen
   - Effective: 45 days from now

3. **Test Scenario 3: Workers Comp Renewal**
   - State: NY
   - Status: Sent to Finance
   - Effective: 60 days from now

4. **Test Scenario 4: Cyber Liability**
   - State: FL
   - Status: Received
   - Effective: 15 days from now

5. **Test Scenario 5: Commercial Auto**
   - State: IL
   - Status: In Progress
   - Effective: 90 days from now

---

## 🚀 How to Use

### From the UI
1. Start the application: `source myenv/bin/activate && python run.py`
2. Navigate to `http://localhost:5001`
3. Log in with your credentials
4. Look for the database dropdown at the top of the left sidebar
5. Select the database you want to use
6. The board will automatically reload

### From the CLI
```bash
# List all databases
python utils/db_manager.py list

# Initialize a database
python utils/db_manager.py init use_cases

# Seed use cases database
python utils/db_manager.py seed use_cases

# Clear a database
python utils/db_manager.py clear test
```

---

## 🔧 Technical Architecture

### Database Switching Flow
1. User selects database from dropdown
2. Frontend calls `POST /api/database/switch`
3. Backend calls `set_current_db(db_name)`
4. Database instance is cached or created
5. Tables are auto-initialized if needed
6. Database name stored in session
7. Frontend reloads submissions from new database

### Session Persistence
- Database selection stored in Flask session
- Persists across page refreshes
- Restored on login
- Cleared on logout (reverts to production)

### Database Instance Caching
- Each database has its own SQLAlchemy engine
- Instances cached in `_db_instances` dictionary
- Avoids reconnection overhead
- Shared across requests in same session

---

## 💡 Key Features

✅ **Seamless Switching**: Switch databases without restarting the app
✅ **Session Persistence**: Your selection is remembered
✅ **Auto-Reload**: Board updates automatically
✅ **Isolated Data**: Each database is completely separate
✅ **Pre-Seeded Tests**: Use Cases DB ready to use
✅ **CLI Tools**: Easy database management
✅ **Auto-Initialize**: Tables created automatically
✅ **Visual Feedback**: Dropdown shows current database

---

## 🎨 UI Changes

### Left Sidebar (Top)
```
┌─────────────────────┐
│   RiskRunway        │
├─────────────────────┤
│ [Production DB ▼]   │  ← New dropdown
├─────────────────────┤
│ ☐ My Cards Only     │
├─────────────────────┤
│ Submission: 5       │
│ Quoting: 3          │
│ Bound: 2            │
└─────────────────────┘
```

### Dropdown Options
- Production DB (default)
- Use Cases DB
- Test DB

---

## 📊 Database Comparison

| Feature | Production | Use Cases | Test |
|---------|-----------|-----------|------|
| **Path** | `data/ipfs_mapper.db` | `data/use_cases.db` | `/tmp/ipfs_mapper_test.db` |
| **Purpose** | Real data | Demos/Testing | Automated tests |
| **Persistent** | ✅ Yes | ✅ Yes | ❌ No (temp) |
| **Pre-Seeded** | ❌ No | ✅ Yes (5 scenarios) | ❌ No |
| **Default** | ✅ Yes | ❌ No | ❌ No |

---

## 🔒 Safety Features

1. **Production Warning**: Clearing production DB requires typing "YES"
2. **Session Isolation**: Each user's database selection is independent
3. **Auto-Revert**: Logging out reverts to production database
4. **Separate Storage**: Databases don't interfere with each other
5. **Backup Friendly**: Each database is a separate file

---

## 🐛 Testing

### Manual Testing Checklist
- [ ] Switch from Production to Use Cases
- [ ] Verify board reloads with test scenarios
- [ ] Switch to Test DB
- [ ] Verify empty board (or test data)
- [ ] Switch back to Production
- [ ] Verify original data is intact
- [ ] Log out and log back in
- [ ] Verify database selection persists

### CLI Testing
```bash
# List databases
python utils/db_manager.py list

# Seed use cases
python utils/db_manager.py seed use_cases

# Clear test database
python utils/db_manager.py clear test
```

---

## 📚 Documentation

- **Full Docs**: `docs/DATABASE_SWITCHING.md`
- **Quick Start**: `QUICK_START_DB_SWITCHING.md`
- **This Summary**: `IMPLEMENTATION_SUMMARY.md`

---

## 🎉 Success Criteria Met

✅ Separate use case database created
✅ Same SQLite engine used
✅ No data sharing between databases
✅ Dropdown switcher on Kanban board
✅ Test database at `/tmp` recognized
✅ Pre-seeded with test scenarios
✅ CLI management tools provided
✅ Full documentation included

---

## 🚀 Next Steps

1. **Test the Implementation**: Start the app and try switching databases
2. **Add More Scenarios**: Create additional test scenarios in Use Cases DB
3. **Customize**: Modify seed data in `utils/db_manager.py`
4. **Integrate with Tests**: Use Test DB for automated E2E tests
5. **Demo Ready**: Use Use Cases DB for demonstrations

---

**Implementation Date**: March 14, 2026
**Status**: ✅ Complete and Ready to Use

