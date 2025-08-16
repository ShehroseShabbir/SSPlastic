# Polyroll Management System

A Django-based management system for handling customer orders, invoices, material tracking, and reporting.  
Built for internal use but structured for collaboration, testing, and production deployment.

---

## 🚀 Features

- Customer and Order Management
- Invoice & Statement Generation (PDF export)
- Material Ledger & Stock Tracking
- Employee Attendance & Expense Tracking
- Dashboard & Reports
- Multi-tenant Site Settings (company details, logo, bank info)

---

## 🛠 Tech Stack

- **Backend:** Django (Python 3.13)
- **Database:** SQLite (dev) / PostgreSQL (recommended for prod)
- **Frontend:** Django Templates, Bootstrap
- **Deployment:** Gunicorn + Nginx on Ubuntu Server
- **Version Control:** Git + GitHub

---

## 📂 Project Structure
polyroll_mgmt/ # Django project config (settings, urls, wsgi/asgi)
core/ # Main application (models, views, utils, templates)
invoices/ # Generated invoices & statements (ignored in Git)
media/ # Uploaded files (ignored in Git)
templates/core/ # Django templates (UI pages)


---

## ⚙️ Setup & Installation

### 1. Clone the repository
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

### 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate   # Linux/Mac
venv\Scripts\activate      # Windows PowerShell

3. Install dependencies
pip install -r requirements.txt

4. Run migrations
python manage.py migrate

5. Create a superuser
python manage.py createsuperuser

6. Start the development server
python manage.py runserver

Open in browser → http://127.0.0.1:8000
```

🔑 Environment Variables
Create a .env file in the project root:
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost

For production:
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,server_ip

🤝 Contributing

Fork the repo
Create a feature branch (git checkout -b feature/new-stuff)
Commit changes (git commit -m "Add new stuff")
Push to branch (git push origin feature/new-stuff)
Open a Pull Request 🚀

📜 License

This project is private. All rights reserved.
For collaboration requests, please contact the repository owner.




