import logging
import sys

def configure_logging():
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    main_logger = logging.getLogger('main_logger')
    main_logger.setLevel(logging.INFO)
    main_logger.addHandler(stream_handler)

    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_logger.addHandler(stream_handler)

    # Optional: also log to a file
    file_handler = logging.FileHandler('/tmp/app.log')
    file_handler.setFormatter(formatter)
    main_logger.addHandler(file_handler)
    error_logger.addHandler(file_handler)
