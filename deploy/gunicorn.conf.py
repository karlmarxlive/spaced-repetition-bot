from config.logging import LOGGING
bind = '0.0.0.0:8080'
workers = 1
threads = 1
timeout = 45
graceful_timeout = 20
accesslog = None
errorlog = '-'
logconfig_dict = LOGGING
