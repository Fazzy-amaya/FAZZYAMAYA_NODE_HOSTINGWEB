==> 
==> ///////////////////////////////////////////////////////////
==> 
==> Available at your primary URL https://fazzyamaya-node-hostingweb.onrender.com
==> 
==> ///////////////////////////////////////////////////////////
[2026-04-20 05:18:51,890] ERROR in app: Exception on /dashboard [GET]
Traceback (most recent call last):
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 1463, in wsgi_app
    response = self.full_dispatch_request()
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 872, in full_dispatch_request
    rv = self.handle_user_exception(e)
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 870, in full_dispatch_request
    rv = self.dispatch_request()
         ^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 855, in dispatch_request
    return self.ensure_sync(self.view_functions[rule.endpoint])(**view_args)  # type: ignore[no-any-return]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/app.py", line 167, in wrapper
    return fn(*a, **kw)
           ^^^^^^^^^^^^
  File "/app/app.py", line 395, in dashboard
    if u.last_spin_date != today:
       ^^^^^^^^^^^^^^^^
AttributeError: 'User' object has no attribute 'last_spin_date'
[2026-04-20 05:19:06,797] ERROR in app: Exception on /dashboard [GET]
Traceback (most recent call last):
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 1463, in wsgi_app
    response = self.full_dispatch_request()
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 872, in full_dispatch_request
    rv = self.handle_user_exception(e)
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 870, in full_dispatch_request
    rv = self.dispatch_request()
         ^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/site-packages/flask/app.py", line 855, in dispatch_request
    return self.ensure_sync(self.view_functions[rule.endpoint])(**view_args)  # type: ignore[no-any-return]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/app.py", line 167, in wrapper
    return fn(*a, **kw)
           ^^^^^^^^^^^^
  File "/app/app.py", line 395, in dashboard
    if u.last_spin_date != today:
       ^^^^^^^^^^^^^^^^
AttributeError: 'User' object has no attribute 'last_spin_date'
