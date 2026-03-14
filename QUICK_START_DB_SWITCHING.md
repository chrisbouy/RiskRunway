# Quick Start: Database Switching

## ✅ What's Been Implemented

You now have a **database switcher** on your Kanban board that allows you to switch between:
1. **Production DB** - Your main database (`data/ipfs_mapper.db`)
2. **Use Cases DB** - Test scenarios database (`data/use_cases.db`)
3. **Test DB** - Automated testing database (`/tmp/ipfs_mapper_test.db`)

## 🎯 How to Use

### 1. Start the Application
```bash
source myenv/bin/activate
python run.py
```

### 2. Log In
- Navigate to `http://localhost:5001`
- Log in with your credentials

### 3. Switch Databases
- Look at the **top of the left sidebar** on the Kanban board
- You'll see a dropdown labeled with the current database
- Click it and select:
  - **Production DB** (default)
  - **Use Cases DB** (test scenarios)
  - **Test DB** (automated tests)
- The board will automatically reload with data from the selected database

### 4. Use the Test Database
The **Use Cases DB** comes pre-seeded with 5 test scenarios. To log in:
- **Username**: `test_user`
- **Password**: `test123`

## 🛠️ Database Management Commands

### List All Databases
```bash
python utils/db_manager.py list
```

### Initialize a Database
```bash
python utils/db_manager.py init use_cases
```

### Seed Use Cases Database
```bash
python utils/db_manager.py seed use_cases
```

### Clear a Database
```bash
python utils/db_manager.py clear test
```

## 📊 What's in the Use Cases Database?

5 pre-seeded test scenarios:
1. **Simple GL Quote** - CA, In Progress
2. **Multi-Coverage Package** - TX, Chosen
3. **Workers Comp Renewal** - NY, Sent to Finance
4. **Cyber Liability** - FL, Received
5. **Commercial Auto** - IL, In Progress

## 🔧 Technical Changes Made

### Files Modified:
1. **config.py** - Added `DATABASES` dictionary with 3 database paths
2. **app/database.py** - Added database switching functions:
   - `get_current_db_name()`
   - `set_current_db(db_name)`
   - `get_available_databases()`
3. **app/routes.py** - Added API endpoints:
   - `GET /api/database/current`
   - `POST /api/database/switch`
4. **app/templates/kanban.html** - Added:
   - Database dropdown in left sidebar
   - JavaScript for database switching
   - Auto-reload on database change

### Files Created:
1. **utils/db_manager.py** - CLI tool for database management
2. **docs/DATABASE_SWITCHING.md** - Full documentation
3. **QUICK_START_DB_SWITCHING.md** - This file

## 🎨 UI Changes

The database dropdown appears at the **top of the left sidebar**, styled with:
- Blue border to make it prominent
- Hover effects
- Auto-updates when you switch databases
- Persists your selection in the session

## 💡 Key Features

✅ **Session Persistence** - Your database selection is saved in your session
✅ **Auto-Reload** - Board automatically reloads when you switch databases
✅ **Isolated Data** - Each database is completely separate
✅ **Pre-Seeded Test Data** - Use Cases DB comes with 5 test scenarios
✅ **CLI Management** - Easy command-line tools for database operations

## 🚀 Next Steps

1. **Try it out**: Start the app and switch between databases
2. **Add more test scenarios**: Use the UI to add submissions to the Use Cases DB
3. **Create custom scenarios**: Seed your own test data using the db_manager.py script
4. **Use for demos**: Switch to Use Cases DB when demonstrating the app

## 📝 Notes

- **Production DB** is the default - your real data is safe
- **Test DB** at `/tmp` may be cleared on system restart (intentional)
- **Use Cases DB** persists between restarts
- Database selection is **per-session** - logging out resets to Production

## 🐛 Troubleshooting

**Database not switching?**
- Check browser console for errors
- Refresh the page
- Log out and log back in

**Missing test scenarios?**
```bash
python utils/db_manager.py seed use_cases
```

**Want to start fresh?**
```bash
python utils/db_manager.py clear use_cases
python utils/db_manager.py seed use_cases
```

---

For full documentation, see: `docs/DATABASE_SWITCHING.md`

