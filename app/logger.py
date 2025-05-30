import logging
import sys
import os

def configure_logging():
    os.makedirs('logs', exist_ok=True)

    formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')

    # Stream (console)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # Main logger
    main_logger = logging.getLogger('main_logger')
    main_logger.setLevel(logging.INFO)
    main_logger.addHandler(stream_handler)
    main_file_handler = logging.FileHandler('logs/info.log')
    main_file_handler.setFormatter(formatter)
    main_logger.addHandler(main_file_handler)

    # Error logger
    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_logger.addHandler(stream_handler)
    error_file_handler = logging.FileHandler('logs/errors.log')
    error_file_handler.setFormatter(formatter)
    error_logger.addHandler(error_file_handler)
