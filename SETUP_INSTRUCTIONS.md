# InsightFolio Backend - Setup Instructions

## Prerequisites

Before setting up the backend, ensure you have the following installed:

- **Python 3.11 or higher** - Download from [python.org](https://www.python.org/downloads/)
- **MySQL Database** (Railway recommended) - For user data, transactions, stocks
- **MongoDB Atlas Account** - For market data and stock information
- **Git** - For cloning the repository

---

## Installation Steps

### 1. Clone the Repository

```bash
git clone https://github.com/InsightFolio/InsightFolio-BE.git
cd InsightFolio-BE
```

### 2. Install Backend Dependencies

**On Windows (PowerShell):**
```powershell
.\install_dependencies.ps1
```

**On Linux/Mac (Bash):**
```bash
chmod +x install_dependencies.sh
./install_dependencies.sh
```

This will install all required Python libraries including:
- Flask and extensions (JWT, CORS, SQLAlchemy)
- Database drivers (PyMySQL, pymongo, mongoengine)
- Data science libraries (pandas, numpy, scikit-learn)
- Financial libraries (yfinance, qlib)
- Testing framework (pytest)

### 3. Set Up Environment Variables

Create a `.env` file in the `flask_app/` directory with your configuration:

```bash
cd flask_app
```

Create `.env` file with the following content:

```env
# Database Configuration
DATABASE_URL=mysql+pymysql://username:password@host:port/database
MONGO_URI=mongodb+srv://username:password@cluster.mongodb.net/?appName=YourApp

# JWT Secret Key
JWT_SECRET_KEY=your-secret-key-here

# Flask Configuration
FLASK_ENV=development
FLASK_DEBUG=True
```

**Replace the placeholders:**
- `DATABASE_URL`: Your Railway MySQL connection string
- `MONGO_URI`: Your MongoDB Atlas connection string
- `JWT_SECRET_KEY`: Generate a secure random key

### 4. Initialize the Database

Run the table creation script to set up MySQL tables:

```bash
cd ..
python recreate_railway_tables.py
```

Type `yes` when prompted to create the tables.

### 5. (Optional) Create Test Accounts

Generate 5 test accounts with a year of trading data:

```bash
python create_test_accounts.py
```

This creates accounts for testing:
- alice@test.com (Password123!)
- bob@test.com (Password123!)
- carol@test.com (Password123!)
- dave@test.com (Password123!)
- eve@test.com (Password123!)

### 6. Run the Backend Server

```bash
cd flask_app
python run.py
```

The backend will start on `http://127.0.0.1:5001`

---

## Verify Installation

**Test the backend is running:**

1. Open your browser and go to: `http://127.0.0.1:5001`
2. You should see a response from the Flask server

**Test endpoints:**
- Health check: `GET http://127.0.0.1:5001/`
- Register: `POST http://127.0.0.1:5001/register`
- Login: `POST http://127.0.0.1:5001/login`

---

## Running Tests

To run the test suite:

```bash
cd flask_app
python -m pytest tests/ -v
```

Run specific test files:
```bash
python -m pytest tests/test_daily_scoring_features.py -v
```

---

## Common Issues

### Issue: MongoDB Connection Error
**Error:** `mongoengine.connection.ConnectionFailure`

**Solution:** Check your `.env` file has `MONGO_URI` (not `MONGODB_URL`)

### Issue: MySQL Connection Failed
**Error:** `Can't connect to MySQL server`

**Solution:** 
- Verify your Railway database credentials
- Check if Railway database is running
- Ensure firewall allows the connection

### Issue: Module Not Found
**Error:** `ModuleNotFoundError: No module named 'flask'`

**Solution:** Re-run the installation script:
```bash
.\install_dependencies.ps1  # Windows
./install_dependencies.sh   # Linux/Mac
```

### Issue: Port Already in Use
**Error:** `Address already in use`

**Solution:** Kill the process using port 5001:
```bash
# Windows
netstat -ano | findstr :5001
taskkill /PID <process_id> /F

# Linux/Mac
lsof -i :5001
kill -9 <process_id>
```

---

## Project Structure

```
InsightFolio-BE/
├── flask_app/
│   ├── app/
│   │   ├── __init__.py          # Flask app initialization
│   │   ├── routes.py            # API endpoints
│   │   ├── models.py            # Database models
│   │   ├── services.py          # Business logic
│   │   └── extensions.py        # Flask extensions
│   ├── tests/                   # Test files
│   ├── .env                     # Environment variables (create this)
│   └── run.py                   # Application entry point
├── requirements.txt             # Python dependencies
├── install_dependencies.ps1     # Windows installer
├── install_dependencies.sh      # Linux/Mac installer
├── recreate_railway_tables.py   # Database setup script
└── create_test_accounts.py      # Test data generator
```

---

## API Documentation

### Authentication Endpoints

**POST /register**
```json
{
  "username": "john_doe",
  "email": "john@example.com",
  "password": "SecurePass123!",
  "risk_averse": "no"
}
```

**POST /login**
```json
{
  "email": "john@example.com",
  "password": "SecurePass123!"
}
```

### Protected Endpoints (Requires JWT Token)

**GET /account** - Get user account details

**GET /holdings** - Get user's stock holdings

**GET /transactions** - Get transaction history

**POST /transactions** - Buy/sell stocks

**GET /stocks** - Search and browse stocks

**GET /stocks/{symbol}** - Get specific stock details

**GET /stockbyscore** - Get stocks ranked by score

For complete API documentation, see the routes.py file.

---

## Development Workflow

1. **Make changes** to code in `flask_app/app/`
2. **Test changes** with `pytest tests/`
3. **Commit changes** to git
4. **Push to GitHub** for deployment

---

## Need Help?

- Check the code comments in `routes.py` and `models.py`
- Review test files in `tests/` for usage examples
- Ensure `.env` file has correct database credentials
- Verify all dependencies are installed with `pip list`

---

## Next Steps

After setting up the backend:

1. **Set up the frontend** in a separate repository
2. **Configure CORS** in `app/__init__.py` to allow frontend requests
3. **Deploy to production** (Railway, Heroku, or similar)
4. **Set up CI/CD** for automated testing and deployment

---

*Last updated: December 5, 2025*
