# ☁ DESI CLOUD

### Advanced Telegram File Sharing & Cloud Storage Bot

DESI CLOUD is a Telegram-powered cloud storage and file-sharing platform.

Upload, manage, share and access your files anytime through Telegram.

---

## ✨ Features

### 👤 User Features

- Telegram user registration
- Custom welcome message
- File upload support
- Document management
- Video and photo support
- Audio and voice support
- Video note and animation support
- Personal file library
- File sharing links
- Unique file tokens
- Rename uploaded files
- Disable file sharing
- Delete uploaded files
- File views and download-request statistics
- Pagination for large file lists
- Force subscription verification

### 🛠 Admin Features

- Admin-only control panel
- User statistics
- File statistics
- Uploaded file management
- Search uploaded files
- Delete files
- Disable sharing
- Force join management
- Add and remove channels
- Enable and disable force join
- Broadcast text messages
- Broadcast Telegram media
- Custom start message
- Custom help message
- Custom about message
- Custom star message

### 🌐 Website Features

- Premium dark interface
- Responsive mobile design
- Public statistics dashboard
- Shared file information page
- Telegram file access button
- Custom 404 page

### 🔐 Security

- Telegram webhook secret validation
- Admin authorization
- File ownership verification
- Secure sharing tokens
- Parameterized SQL queries
- PostgreSQL transactions
- Input validation
- HTML escaping
- Duplicate update protection

---

# 🧰 Technology Stack

| Technology | Purpose |
|---|---|
| Python 3.11+ | Backend |
| python-telegram-bot 22.5 | Telegram Bot API |
| Flask 3.1.2 | Web server |
| PostgreSQL | Database |
| psycopg2-binary | PostgreSQL driver |
| Gunicorn 23.0.0 | Production server |
| HTML5 | Website |
| CSS3 | Website design |
| JavaScript | Frontend interactions |
| Render | Hosting |
| GitHub | Source code |

---

# 📁 Project Structure

```text
DESI-CLOUD/
│
├── bot.py
├── config.py
├── database.py
├── handlers.py
├── keyboards.py
├── utils.py
│
├── requirements.txt
├── runtime.txt
├── .env.example
├── .gitignore
├── Procfile
├── README.md
│
├── database/
│   ├── schema.sql
│   └── migrations/
│       └── 001_initial_schema.sql
│
├── templates/
│   ├── index.html
│   ├── dashboard.html
│   ├── file.html
│   └── 404.html
│
└── static/
    ├── css/
    │   └── style.css
    │
    ├── js/
    │   └── app.js
    │
    └── images/
        └── logo.svg
