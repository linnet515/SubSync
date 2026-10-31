# SubSync: Testing (Support)

## Approach
- Manual testing of every feature as admin, staff and customer.
- Backend checks through the /api/... addresses.
- Test checklist of 43 cases, grouped by feature. Bugs are logged and sent to the file owner.

## Tested so far
- Sign-up: unique email, password rules (more than 8 characters, upper, lower, special)
- Login for the three roles, wrong password, logout (server answers "Please log in" after logout)
- Password reset: link is printed in the terminal when email is not configured

## Demo logins
- admin@brightloop.com / Admin@12345
- staff@brightloop.com / Staff@12345
- customer@acme.com / Customer@123

## How to run
python app.py, then open http://127.0.0.1:5000