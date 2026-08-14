import time
import threading
from flask import Flask, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS

from config import (
    TOSS_CLIENT_ID,
    TOSS_CLIENT_SECRET,
    TOSS_ACCOUNT_SEQ,
    ENABLE_LIVE_TRADING,
    TOSS_DASHBOARD_PORT,
)
from core.client import TossOpenAPIClient
from core.bot import Quantitative24HourBot
from utils.logger import configure_socket_logger

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Setup Logger to stream to WebSockets
configure_socket_logger(socketio)

# Global State
bot_thread = None
stop_event = threading.Event()

def bot_state_callback(state_dict):
    """Callback fired by the bot every iteration. Pushes data to React."""
    socketio.emit("system_state", state_dict)

def run_bot_loop():
    """Background Daemon Loop"""
    toss_client = TossOpenAPIClient(
        client_id=TOSS_CLIENT_ID,
        client_secret=TOSS_CLIENT_SECRET,
        account_seq=TOSS_ACCOUNT_SEQ,
        dry_run=not ENABLE_LIVE_TRADING
    )
    
    bot = Quantitative24HourBot(
        api_client=toss_client, 
        symbol="SOXL", 
        target_size=500,
        on_state_update=bot_state_callback
    )
    
    app.logger.info("Background Trading Engine Started.")
    
    while not stop_event.is_set():
        try:
            session_info = bot.session_manager.get_session_info()
            regime = session_info["regime"]

            bot.run_strategy_iteration()

            # Dynamic Polling Interval
            if regime in ["REGULAR", "PRE_MARKET"]:
                time.sleep(0.25)
            elif regime in ["DAYTIME_ATS", "AFTER_MARKET"]:
                time.sleep(1.0)
            else:
                time.sleep(10.0)
                
        except Exception as e:
            app.logger.error(f"Engine Loop Error: {str(e)}", exc_info=True)
            time.sleep(5)  # Prevent rapid fail-loop

@app.route("/api/status", methods=["GET"])
def get_status():
    is_running = bot_thread is not None and bot_thread.is_alive()
    return jsonify({"running": is_running})

@app.route("/api/start", methods=["POST"])
def start_bot():
    global bot_thread, stop_event
    if bot_thread is not None and bot_thread.is_alive():
        return jsonify({"message": "Bot is already running", "running": True}), 400
        
    stop_event.clear()
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"message": "Bot started successfully", "running": True})

@app.route("/api/stop", methods=["POST"])
def stop_bot():
    global stop_event
    stop_event.set()
    return jsonify({"message": "Stop signal sent to bot", "running": False})

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=TOSS_DASHBOARD_PORT,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )