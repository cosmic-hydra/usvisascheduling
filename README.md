# usvisascheduling Setup Guide (Termux + Windows CMD)

This guide is a full from-scratch tutorial for running this project on:

- Termux (Android)
- Windows Command Prompt (CMD)

You will learn how to:

1. Install all dependencies
2. Clone and set up the project
3. Configure environment variables with a .env file
4. Run each script (solver, service, client, slot monitor)
5. Fix common errors quickly

---

## What Is In This Repo

- solver.py: solves one Cloudflare Turnstile challenge and prints a token
- service.py: starts a local HTTP API server to solve multiple requests
- clientsend.py: sends a request to service.py from terminal
- usvisa_slot_monitor.py: logs in and checks US visa slots automatically

---

## Important Compatibility Notes

- Windows CMD: fully supported.
- Termux: best for Python scripts and API/client flow.
- usvisa_slot_monitor.py uses Playwright browser automation and may be limited on pure Android Termux without a full desktop-like browser environment.

If you need the most reliable slot monitor execution, use Windows or a Linux VPS.

---

## Part 1: Termux Setup (Android)

### 1. Install Termux

Install Termux from F-Droid (recommended). Open Termux and run:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python git curl
```

Check Python:

```bash
python --version
pip --version
```

### 2. Clone Project

```bash
git clone https://github.com/cosmic-hydra/usvisascheduling.git
cd usvisascheduling
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create .env File

Create .env in the project root:

```bash
cat > .env << 'EOF'
USVISA_USERNAME=your_username
USVISA_PASSWORD=your_password
USVISA_Q1=your_answer_1
USVISA_Q2=your_answer_2
USVISA_Q3=your_answer_3
EOF
```

Optional settings:

```bash
cat >> .env << 'EOF'
PORT=8191
MAX_WORKERS=2
EOF
```

### 5. Run Scripts In Termux

Single solve:

```bash
python solver.py YOUR_SITEKEY https://example.com/
```

Start API service:

```bash
python service.py
```

In another Termux session, send request:

```bash
python clientsend.py YOUR_SITEKEY https://example.com/ 45
```

Run visa monitor:

```bash
python usvisa_slot_monitor.py
```

If slot monitor fails in Termux because browser automation is unavailable, run it on Windows CMD instead.

---

## Part 2: Windows CMD Setup (From Scratch)

### 1. Install Python

1. Download Python 3.10+ from python.org
2. During install, enable Add Python to PATH
3. Open CMD and verify:

```cmd
python --version
pip --version
```

### 2. Install Git

Install Git for Windows, then verify:

```cmd
git --version
```

### 3. Clone Project

```cmd
git clone https://github.com/cosmic-hydra/usvisascheduling.git
cd usvisascheduling
```

### 4. Install Dependencies

```cmd
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 5. Create .env File In CMD

Run these lines exactly in CMD:

```cmd
(
echo USVISA_USERNAME=your_username
echo USVISA_PASSWORD=your_password
echo USVISA_Q1=your_answer_1
echo USVISA_Q2=your_answer_2
echo USVISA_Q3=your_answer_3
) > .env
```

Append optional variables:

```cmd
(
echo PORT=8191
echo MAX_WORKERS=4
) >> .env
```

### 6. Run Scripts In CMD

Single solve:

```cmd
python solver.py YOUR_SITEKEY https://example.com/
```

Start API service:

```cmd
python service.py
```

Send request from another CMD window:

```cmd
python clientsend.py YOUR_SITEKEY https://example.com/ 45
```

Run visa monitor:

```cmd
python usvisa_slot_monitor.py
```

### 7. One-Click Run (Windows)

You can also use:

```cmd
run.bat
```

This checks Python, installs dependencies, and starts usvisa_slot_monitor.py.

---

## Part 3: Environment Variables Reference

Required for visa monitor:

- USVISA_USERNAME: login username or email
- USVISA_PASSWORD: account password
- USVISA_Q1: security question answer 1
- USVISA_Q2: security question answer 2
- USVISA_Q3: security question answer 3

Common optional:

- USVISA_POSTS: comma-separated posts list
- TELEGRAM_BOT_TOKEN: Telegram bot token
- TELEGRAM_CHAT_ID: Telegram chat id
- AUTO_BOOK: true or false
- PORT: API service port (default 8191)
- MAX_WORKERS: concurrent solve workers (default 4)

---

## Part 4: API Usage

Start service:

```bash
python service.py
```

Send request with curl:

```bash
curl -s -X POST http://127.0.0.1:8191/solve \
  -H "Content-Type: application/json" \
  -d '{"sitekey":"YOUR_SITEKEY","siteurl":"https://example.com/","timeout":45}'
```

Health check:

```bash
curl http://127.0.0.1:8191/health
```

---

## Part 5: Troubleshooting

Python command not found (Windows):

- Reinstall Python and enable Add Python to PATH

pip install fails:

- Run python -m pip install --upgrade pip
- Retry with stable internet

Cannot reach service at 127.0.0.1:8191:

- Make sure python service.py is running in another terminal

No token returned before timeout:

- Increase timeout value in clientsend.py command

Termux browser/automation issues:

- Use Windows CMD or Linux VPS for full browser automation workloads

---

## Quick Start Cheat Sheet

Termux quick run:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python git
git clone https://github.com/cosmic-hydra/usvisascheduling.git
cd usvisascheduling
pip install -r requirements.txt
python service.py
```

Windows CMD quick run:

```cmd
git clone https://github.com/cosmic-hydra/usvisascheduling.git
cd usvisascheduling
python -m pip install -r requirements.txt
python service.py
```
