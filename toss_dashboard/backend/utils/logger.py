import logging

class SocketIOLogHandler(logging.Handler):
    """Custom logging handler that emits log records via WebSockets."""
    def __init__(self, socketio_instance):
        super().__init__()
        self.sio = socketio_instance

    def emit(self, record):
        try:
            log_entry = self.format(record)
            # Emit to the 'trading_logs' event room
            self.sio.emit('trading_logs', {'message': log_entry})
        except Exception:
            self.handleError(record)

def configure_socket_logger(sio):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers if reloading
    if not any(isinstance(h, SocketIOLogHandler) for h in logger.handlers):
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
        
        # Keep console output
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        
        # Add WebSocket output
        sio_handler = SocketIOLogHandler(sio)
        sio_handler.setFormatter(formatter)
        logger.addHandler(sio_handler)
