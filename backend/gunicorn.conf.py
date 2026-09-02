bind = "0.0.0.0:5000"
# Один worker обязателен, пока scheduler живет внутри процесса приложения.
workers = 1
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "info"
