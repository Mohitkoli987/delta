from flask import Flask, render_template, request, jsonify
import requests
import time
import hmac
import hashlib
import json
import threading
from threading import Lock
import os
from datetime import datetime
from dotenv import load_dotenv
import pymysql
from math import isfinite
import subprocess
from decimal import Decimal, ROUND_HALF_UP

# Load environment variables
load_dotenv()

# Import keepalive functionality
from keepalive import start_keep_alive

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ========== MYSQL CONNECTION MANAGER ==========
def get_mysql_connection():
    """Create and return a MySQL connection"""
    try:
        connection = pymysql.connect(
            host=os.getenv('MYSQL_HOST', 'bmh1rsh5f0sjmncv6ydc-mysql.services.clever-cloud.com'),
            port=int(os.getenv('MYSQL_PORT', 3306)),
            user=os.getenv('MYSQL_USER', 'ujokhsx1defubkot'),
            password=os.getenv('MYSQL_PASSWORD', 'hILZGFpJ60exq4oGj2hv'),
            database=os.getenv('MYSQL_DB', 'bmh1rsh5f0sjmncv6ydc'),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            ssl={}  # ✅ Yeh add karo
        )
        return connection
    except Exception as e:
        print(f"❌ MySQL connection failed: {e}")
        raise

def execute_mysql_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    """Execute MySQL query with proper error handling"""
    connection = None
    try:
        connection = get_mysql_connection()
        with connection.cursor() as cursor:
            cursor.execute(query, params or ())
            
            if commit:
                connection.commit()
                return cursor.lastrowid if hasattr(cursor, 'lastrowid') else True
            
            if fetch_one:
                return cursor.fetchone()
            elif fetch_all:
                return cursor.fetchall()
            else:
                return True
                
    except Exception as e:
        if connection:
            connection.rollback()
        print(f"❌ MySQL query failed: {e}")
        raise
    finally:
        if connection:
            connection.close()

def get_database_size():
    """Get current database size in MB"""
    try:
        query = """
        SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS db_size_mb
        FROM information_schema.tables 
        WHERE table_schema = DATABASE()
        """
        result = execute_mysql_query(query, fetch_one=True)
        return result['db_size_mb'] if result else 0
    except Exception as e:
        print(f"❌ Error getting database size: {e}")
        return 0

def cleanup_old_trades(target_size_mb=8.5):
    """Remove oldest trades to keep database under target size (default 8.5MB to stay under 9MB limit)"""
    try:
        current_size = get_database_size()
        print(f"📊 Current database size: {current_size}MB")
        
        if current_size <= target_size_mb:
            print(f"✅ Database size is under limit ({current_size}MB <= {target_size_mb}MB)")
            return True
        
        print(f"⚠️ Database size exceeds limit ({current_size}MB > {target_size_mb}MB)")
        print("🧹 Starting cleanup of oldest trades...")
        
        # Calculate how many rows to delete (delete in batches)
        while current_size > target_size_mb:
            # Get count of rows to delete (delete 10% at a time)
            count_query = "SELECT COUNT(*) as total_rows FROM closed_positions"
            total_result = execute_mysql_query(count_query, fetch_one=True)
            total_rows = total_result['total_rows'] if total_result else 0
            
            if total_rows <= 10:  # Keep at least 10 rows
                print("⚠️ Only 10 or fewer rows remaining, stopping cleanup")
                break
            
            rows_to_delete = max(1, total_rows // 10)  # Delete 10% of rows
            
            # Delete oldest rows
            delete_query = """
            DELETE FROM closed_positions 
            ORDER BY created_at ASC 
            LIMIT %s
            """
            execute_mysql_query(delete_query, (rows_to_delete,), commit=True)
            
            # Check new size
            current_size = get_database_size()
            print(f"🗑️ Deleted {rows_to_delete} oldest rows, new size: {current_size}MB")
            
            if rows_to_delete == 1 and current_size > target_size_mb:
                print("⚠️ Deleting rows one by one, but still over limit")
                break
        
        print(f"✅ Cleanup completed. Final database size: {current_size}MB")
        return True
        
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        return False

# API Configuration
# BASE_URL = "https://cdn-ind.testnet.deltaex.org"
BASE_URL = "https://api.india.delta.exchange"

WS_URL = "wss://socket.india.delta.exchange"

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

# ---------- LIVE POSITION TP/SL CONFIGURATION ----------
LIVE_TP_PERCENTAGE = 1   # 0.5% Take Profit
LIVE_SL_PERCENTAGE = 0.5  # 0.25% Stop Loss

processing_lock = threading.Lock()
last_processed = {}

# Bot State
BOT_STATE = {
    'running': False,
    'thread': None,
    'current_lot': 1,
    'base_lot': 1,  # Integer for size parameter
    'leverage': 10,
    'tp_percent': 2.0,
    'sl_percent': 1.0,
    'max_streak': 5,
    'current_streak': 0,
    'last_result': None,
    'symbol': 'ADAUSD',
    'stop_at_win': False,  # Stop after next profit
    'stop_at_max_streak': False,  # Stop when max loss streak hit
    'force_stop': False,    # Immediate stop
    'session_start_time': None,  # Track when current session started
    'session_total_pnl': 0.0  # Total P&L for current session
}

# Trading Configuration - Market Specific Lot Sizes
LOT_SIZES = {
    'ADAUSD': 1,   # 1 lot = 1 ADA
}

# Default lot size for backward compatibility
LOT_SIZE_DEFAULT = 1  # 1 lot = 1 ADA (easily configurable)

# Trade Result Memory
LAST_TRADE_RESULT = {
    'profit_loss': None,
    'timestamp': None,
    'lot_used': None,
    'processed': False  # Prevent duplicate processing
}

# Lot Calculation Safety
LOT_CALCULATION_LOCK = False
LAST_PROCESSED_TRADE_ID = None

# Recovery System - Track processed exit fill IDs
PROCESSED_EXIT_FILL_IDS = set()



# Position State Tracking
LAST_POSITION_STATE = {
    'symbol': None,
    'size': 0,
    'entry_price': 0
}

# Product ID Cache for Real-time Endpoint
PRODUCT_ID_CACHE = {}


# Database Thread Lock
db_lock = Lock()

# Bot Process Management
BOT_PROCESS = None
bot_process_lock = Lock()

# ========== DATABASE ==========
# def init_database():
#     """Initialize MySQL database for trade storage"""
#     try:
#         # Drop existing table to ensure fresh start
#         execute_mysql_query("DROP TABLE IF EXISTS closed_positions", commit=True)
#         print("🗑️ Dropped existing closed_positions table")
        
#         # Create new table with MySQL syntax
#         create_table_query = '''
#             CREATE TABLE closed_positions (
#                 id INT AUTO_INCREMENT PRIMARY KEY,
#                 symbol VARCHAR(50) NOT NULL,
#                 side VARCHAR(10) NOT NULL,
#                 entry_price DECIMAL(20, 8) NOT NULL,
#                 exit_price DECIMAL(20, 8),
#                 quantity DECIMAL(20, 8) NOT NULL,
#                 pnl DECIMAL(20, 8),
#                 entry_time VARCHAR(50) NOT NULL,
#                 exit_time VARCHAR(50),
#                 is_latest TINYINT(1) DEFAULT 0,
#                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 INDEX idx_symbol (symbol),
#                 INDEX idx_created_at (created_at),
#                 INDEX idx_is_latest (is_latest)
#             ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
#         '''
        
#         execute_mysql_query(create_table_query, commit=True)
#         print("✅ MySQL table 'closed_positions' created successfully")
        
#     except Exception as e:
#         print(f"❌ Failed to initialize MySQL database: {e}")
#         raise



def init_database():
    """Initialize MySQL database - preserves existing data"""
    try:
        # ✅ DROP TABLE HATAO - sirf CREATE IF NOT EXISTS rakho
        create_table_query = '''
            CREATE TABLE IF NOT EXISTS closed_positions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(10) NOT NULL,
                entry_price DECIMAL(20, 8) NOT NULL,
                exit_price DECIMAL(20, 8),
                quantity DECIMAL(20, 8) NOT NULL,
                pnl DECIMAL(20, 8),
                entry_time VARCHAR(50) NOT NULL,
                exit_time VARCHAR(50),
                is_latest TINYINT(1) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_symbol (symbol),
                INDEX idx_created_at (created_at),
                INDEX idx_is_latest (is_latest)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        '''
        execute_mysql_query(create_table_query, commit=True)
        print("✅ MySQL table ready (existing data preserved)")

    except Exception as e:
        print(f"❌ Failed to initialize MySQL database: {e}")
        raise

# ========== BOT PROCESS MANAGEMENT ==========
def start_signal_bot():
    """Start decision bot as a subprocess"""
    global BOT_PROCESS
    
    with bot_process_lock:
        if BOT_PROCESS and BOT_PROCESS.poll() is None:
            print("⚠️ Decision bot is already running")
            return False
        
        try:
            # Get the directory of this script
            script_dir = os.path.dirname(os.path.abspath(__file__))
            bot_script = os.path.join(script_dir, "decision_bot.py")
            
            # Start decision_bot.py as subprocess
            BOT_PROCESS = subprocess.Popen(
                ['python3', bot_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            print(f"✅ Decision bot started with PID: {BOT_PROCESS.pid}")
            
            # Start a thread to monitor bot output
            def monitor_bot_output():
                for line in iter(BOT_PROCESS.stdout.readline, ''):
                    if line.strip():
                        print(f"[DECISION-BOT] {line.strip()}")
                BOT_PROCESS.stdout.close()
            
            monitor_thread = threading.Thread(target=monitor_bot_output, daemon=True)
            monitor_thread.start()
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to start decision bot: {e}")
            return False

def stop_signal_bot():
    """Stop the decision bot subprocess"""
    global BOT_PROCESS
    
    with bot_process_lock:
        if not BOT_PROCESS or BOT_PROCESS.poll() is not None:
            print("⚠️ Decision bot is not running")
            return False
        
        try:
            BOT_PROCESS.terminate()
            BOT_PROCESS.wait(timeout=5)
            print(f"✅ Decision bot stopped")
            BOT_PROCESS = None
            return True
            
        except subprocess.TimeoutExpired:
            print("⚠️ Decision bot did not stop gracefully, killing...")
            BOT_PROCESS.kill()
            BOT_PROCESS.wait()
            BOT_PROCESS = None
            return True
            
        except Exception as e:
            print(f"❌ Failed to stop decision bot: {e}")
            return False

def save_closed_position(trade_data):
    """Save closed trade to MySQL database with thread safety and size management"""
    print(f"💾 Saving trade to MySQL database: {trade_data}")
    try:
        with db_lock:  # Thread-safe database access
            print(f"📊 MySQL database connecting...")
            
            # Check database size and cleanup if needed (keep under 8.5MB to stay under 9MB limit)
            cleanup_old_trades(target_size_mb=8.5)
            
            # Reset previous latest for this symbol
            execute_mysql_query(
                "UPDATE closed_positions SET is_latest = 0 WHERE symbol = %s",
                (trade_data['symbol'],),
                commit=True
            )
            
            # Insert new trade as latest
            insert_query = '''
                INSERT INTO closed_positions 
                (symbol, side, entry_price, exit_price, quantity, pnl, entry_time, exit_time, is_latest)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
            '''
            
            execute_mysql_query(insert_query, (
                trade_data['symbol'],
                trade_data['side'],
                trade_data['entry_price'],
                trade_data['exit_price'],
                trade_data['quantity'],
                trade_data['pnl'],
                trade_data['entry_time'],
                trade_data['exit_time']
            ), commit=True)
            
            # Show final database size after insertion
            final_size = get_database_size()
            print(f"✅ Trade saved successfully to MySQL database")
            print(f"📊 Final database size: {final_size}MB")
            
    except Exception as e:
        print(f"❌ Error saving trade to MySQL database: {e}")
        import traceback
        traceback.print_exc()

# ========== OPTIMIZED POSITION TRACKING ==========
def get_product_id(symbol):
    """Get product_id for a symbol (cache this for better performance)"""
    global PRODUCT_ID_CACHE
    
    # Check cache first
    if symbol in PRODUCT_ID_CACHE:
        return PRODUCT_ID_CACHE[symbol]
    
    try:
        products = make_api_request('GET', '/products')
        if not products or not products.get('result'):
            return None
        
        for product in products.get('result', []):
            if product.get('symbol') == symbol:
                product_id = product.get('id')
                # Cache the result
                PRODUCT_ID_CACHE[symbol] = product_id
                print(f"✅ Cached product_id for {symbol}: {product_id}")
                return product_id
        
        return None
        
    except Exception as e:
        print(f"❌ Error getting product_id: {e}")
        return None

def check_position_realtime(product_id):
    """Check position with real-time endpoint"""
    try:
        response = make_api_request('GET', f'/positions/margined?product_id={product_id}')
        if not response or not response.get('success'):
            print("⚠️ API FAILED - Returning error state")
            return {'error': True}
        response = make_api_request('GET', f'/positions?product_id={product_id}')
        
        if response and response.get('success') and response.get('result'):
            result = response['result']
            size = float(result.get('size', 0))
            entry_price = float(result.get('entry_price', 0)) if abs(size) > 0.001 else 0
            
            return {
                'has_position': abs(size) > 0.001,  # ✅ Tolerance check to avoid false positives
                'size': size,
                'entry_price': entry_price
            }
        
        return {
            'has_position': False,
            'size': 0,
            'entry_price': 0
        }
        
    except Exception as e:
        print(f"❌ Error checking position: {e}")
        return {
            'has_position': False,
            'size': 0,
            'entry_price': 0
        }


# ========== API FUNCTIONS ==========
def get_server_time():
    """Get server timestamp from Delta Exchange"""
    try:
        response = requests.get(f"{BASE_URL}/v2/time", timeout=5)
        if response.status_code == 200:
            return str(int(response.json()['result']))
        else:
            return str(int(time.time()))
    except:
        return str(int(time.time()))

def sign_request(method, path, body=""):
    """Generate API signature using Delta Exchange format"""
    ts = get_server_time()
    payload = method + ts + path + body
    signature = hmac.new(
        DELTA_API_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "api-key": DELTA_API_KEY,
        "timestamp": ts,
        "signature": signature,
        "Content-Type": "application/json"
    }

def safe_float(value, fallback=0.0):
    """Convert value to float safely"""
    try:
        if value is None or value == "":
            return fallback
        f = float(value)
        if not isfinite(f):
            return fallback
        return f
    except:
        return fallback

def make_api_request(method, endpoint, data=None):
    """Make authenticated API request using Delta Exchange format"""
    path = f"/v2{endpoint}"
    body = json.dumps(data) if data else ""
    
    headers = sign_request(method, path, body)
    url = f"{BASE_URL}{path}"
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            response = requests.post(url, headers=headers, data=body, timeout=10)
        elif method == 'DELETE':
            response = requests.delete(url, headers=headers, data=body, timeout=10)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ API Error: Status {response.status_code}")
            print(f"❌ Error Body: {response.text}")  # Add error body logging
            return None
    except Exception as e:
        print(f"🚨 API Exception: {e}")
        return None


def place_order(symbol, side, quantity, order_type='market_order'):
    """Place order with correct Delta Exchange parameters"""
    order_data = {
        'product_symbol': symbol,  # Only use product_symbol
        'side': side,
        'order_type': order_type,  # Must be 'market_order' not 'market'
        'size': quantity  # ✅ Correct parameter name: 'size' not 'quantity'
    }
    
    print(f"📋 Order Data: {order_data}")
    return make_api_request('POST', '/orders', order_data)

def set_leverage(symbol, leverage):
    """Set leverage using correct Delta Exchange endpoint"""
    # First get product_id from symbol
    products = make_api_request('GET', '/products')
    if not products or not products.get('result'):
        print(f"❌ Failed to fetch products")
        return None
    
    product_id = None
    for product in products.get('result', []):
        if product.get('symbol') == symbol:
            product_id = product.get('id')
            break
    
    if not product_id:
        print(f"❌ Product not found: {symbol}")
        return None
    
    print(f"🔧 Found product_id: {product_id} for {symbol}")
    
    # Use correct endpoint with product_id in path
    return make_api_request('POST', f'/products/{product_id}/orders/leverage', {'leverage': str(leverage)})

def get_wallet_balance():
    """Get wallet balance from Delta Exchange API"""
    try:
        path = "/v2/wallet/balances"
        response = make_api_request('GET', '/wallet/balances')
        
        if response and response.get("success") and response.get("result"):
            balances = response["result"]
            if not isinstance(balances, list):
                balances = [balances] if isinstance(balances, dict) else []
            
            wallet_balance = 0.0
            available_balance = 0.0
            asset_symbol = "USD"
            
            for balance in balances:
                if not isinstance(balance, dict):
                    continue
                asset = (balance.get("asset_symbol") or "").upper()
                if asset in ("USD", "USDT", "USDC"):
                    wallet_balance = safe_float(balance.get("balance"), 0)
                    available_balance = safe_float(balance.get("available_balance"), 0)
                    asset_symbol = asset
                    break
            
            if wallet_balance == 0:
                for balance in balances:
                    if not isinstance(balance, dict):
                        continue
                    bal_val = safe_float(balance.get("balance"), 0)
                    if bal_val > 0:
                        wallet_balance = bal_val
                        available_balance = safe_float(balance.get("available_balance"), 0)
                        asset_symbol = (balance.get("asset_symbol") or "USD")
                        break
            
            margin_used = wallet_balance - available_balance
            
            return {
                'success': True,
                'balance': wallet_balance,
                'available_balance': available_balance,
                'margin_used': margin_used,
                'currency': asset_symbol
            }
        
        # Fallback values
        return {
            'success': True,
            'balance': 10000.0,
            'available_balance': 8500.0,
            'margin_used': 1500.0,
            'currency': 'USDT'
        }
            
    except Exception as e:
        print(f"Wallet balance error: {e}")
        return {
            'success': True,
            'balance': 10000.0,
            'available_balance': 8500.0,
            'margin_used': 1500.0,
            'currency': 'USDT'
        }

# ========== IMPROVED POSITION TRACKING ==========
def check_position_and_detect_closure():
    """OPTIMIZED - Check position using real-time endpoint"""
    global LAST_POSITION_STATE
    
    try:
        print(f"\n{'='*60}")
        print(f"🔍 POSITION CHECK (REAL-TIME)")
        print(f"LAST_POSITION_STATE: {LAST_POSITION_STATE}")
        print(f"Symbol: {BOT_STATE['symbol']}")
        print(f"{'='*60}")
        
        # Get product_id for real-time endpoint
        product_id = get_product_id(BOT_STATE['symbol'])
        if not product_id:
            print("❌ Could not get product_id")
            return False, False, 0
        
        # ✅ Use REAL-TIME endpoint (instant response)
        current_pos = check_position_realtime(product_id)
        
        # 🔥 FIX 1: API FAIL = DO NOTHING
        if current_pos.get('error'):
            print("⚠️ API FAILED - SKIPPING CLOSURE CHECK")
            return True, False, 0  # assume position still exists
        
        # Check if position was closed
        was_closed = False
        pnl = 0
        
        # 🔥 FIX 2: CRASH FIX - Use .get() to avoid KeyError
        if abs(LAST_POSITION_STATE['size']) > 0.001 and abs(current_pos.get('size', 0)) <= 0.001:
            print("🎯 Position CLOSED!")
            
            # Faster fills wait
            time.sleep(0.3)
            
            was_closed = True
            pnl = get_pnl_from_fills()
            
            print(f"     PnL: {pnl:.5f}")
            print(f"     Result: {'PROFIT ' if pnl > 0 else 'LOSS '}{' ' if pnl > 0 else ' '}")
            
            # Exit type detection for decision bot
            entry_exit_data = get_entry_exit_from_fills()
            exit_type = "UNKNOWN"
            
            if entry_exit_data and pnl != 0:
                if pnl > 0:
                    exit_type = "TP"   # Profit = TP hit
                else:
                    exit_type = "SL"   # Loss = SL hit
                
                # Write exit result for decision bot
                _write_exit_result(exit_type, pnl, entry_exit_data)
                print(f"     Exit Type: {exit_type} (written to decision bot)")
            else:
                print(f"     Exit Type: UNKNOWN (no entry/exit data)")     
            LAST_TRADE_RESULT['profit_loss'] = pnl
            LAST_TRADE_RESULT['timestamp'] = datetime.now().isoformat()
            LAST_TRADE_RESULT['lot_used'] = LAST_POSITION_STATE['size']
            BOT_STATE['last_result'] = 'PROFIT' if pnl > 0 else 'LOSS'
            LAST_TRADE_RESULT['processed'] = True  # Mark as processed
            
            # Update session total P&L
            if BOT_STATE['session_start_time']:  # Only update if session is active
                BOT_STATE['session_total_pnl'] += pnl
                print(f"   Session P&L updated: {BOT_STATE['session_total_pnl']:.2f} (Trade P&L: {pnl:.2f})")
            
            entry_exit_data = get_entry_exit_from_fills()
            print(f" Entry/Exit Data: {entry_exit_data}")
            if entry_exit_data:
                print(f" Saving trade to database...")
                save_closed_position({
                    'symbol': BOT_STATE['symbol'],
                    'side': entry_exit_data['side'],
                    'entry_price': entry_exit_data['entry_price'],
                    'exit_price': entry_exit_data['exit_price'],
                    'quantity': entry_exit_data['quantity'],
                    'pnl': pnl,
                    'entry_time': entry_exit_data['entry_time'],
                    'exit_time': entry_exit_data['exit_time']
                })
                print(f"💾 ✅ TRADE SAVED!")
            else:
                print(f"⚠️ No entry/exit data found - trade not saved")
        
        # 🔥 EXTRA: RECOVERY CHECK
        elif LAST_POSITION_STATE['size'] > 0 and current_pos.get('size', 0) == 0:
            print("🔄 RECOVERY: Position was closed during network issue")
        
        # Update position state
        LAST_POSITION_STATE = {
            'symbol': BOT_STATE['symbol'],
            'size': current_pos.get('size', 0),
            'entry_price': current_pos.get('entry_price', 0)
        }
        
        has_position = abs(current_pos.get('size', 0)) > 0.001
        return has_position, was_closed, pnl
        
    except Exception as e:
        print(f"❌ Position processing error: {e}")
        import traceback
        traceback.print_exc()
        return False, False, 0


def get_pnl_from_fills():
    """Get PnL from fills API for the last closed trade"""
    try:
        fills = make_api_request('GET', '/fills?page_size=20')
        if not fills or not fills.get('result'):
            print("❌ No fills data")
            return 0
        
        fills_data = fills.get('result', [])
        
        # Filter fills for our symbol
        symbol_fills = [f for f in fills_data if f.get('product_symbol') == BOT_STATE['symbol']]
        
        print(f"📊 Found {len(symbol_fills)} fills for {BOT_STATE['symbol']}")
        
        if len(symbol_fills) < 2:
            print("❌ Not enough fills to calculate PnL")
            return 0
        
        # Fills are ordered newest first, so:
        # symbol_fills[0] = exit fill (most recent)
        # symbol_fills[1] = entry fill (older)
        exit_fill = symbol_fills[0]
        entry_fill = symbol_fills[1]
        
        print(f"📊 Entry Fill: {entry_fill.get('side')} @ {entry_fill.get('price')} size {entry_fill.get('size')}")
        print(f"📊 Exit Fill: {exit_fill.get('side')} @ {exit_fill.get('price')} size {exit_fill.get('size')}")
        
        # Check if they are opposite sides
        if entry_fill.get('side') == exit_fill.get('side'):
            print("❌ Fills are same side, not a closed trade")
            return 0
        
        entry_price = float(entry_fill.get('price', 0))
        exit_price = float(exit_fill.get('price', 0))
        size_in_lots = float(entry_fill.get('size', 0))
        
        # Get market-specific lot size
        symbol = LAST_POSITION_STATE.get('symbol', 'ADAUSD')
        lot_size = LOT_SIZES.get(symbol, LOT_SIZE_DEFAULT)  # Fallback to default
        
        # Convert lots to actual quantity using market-specific lot size
        actual_quantity = size_in_lots * lot_size
        
        print(f"📊 Size: {size_in_lots} lots = {actual_quantity} {symbol.replace('USD', '')} (1 lot = {lot_size} {symbol.replace('USD', '')})")
        
        # Calculate PnL based on entry side using actual quantity
        if entry_fill.get('side') == 'buy':
            # Long position: profit if exit > entry
            pnl = (exit_price - entry_price) * actual_quantity
        else:
            # Short position: profit if entry > exit
            pnl = (entry_price - exit_price) * actual_quantity
        
        print(f"💰 Calculated PnL: {pnl:.5f}")
        
        return pnl
        
    except Exception as e:
        print(f"❌ Error getting PnL from fills: {e}")
        import traceback
        traceback.print_exc()
        return 0

def get_entry_exit_from_fills():
    """Get entry and exit data from fills API"""
    try:
        print(f"🔍 Getting fills for symbol: {BOT_STATE['symbol']}")
        fills = make_api_request('GET', '/fills?page_size=20')
        if not fills or not fills.get('result'):
            print(f"❌ No fills data received")
            return None
        
        fills_data = fills.get('result', [])
        print(f"📊 Total fills received: {len(fills_data)}")
        
        # Filter fills for our symbol
        symbol_fills = [f for f in fills_data if f.get('product_symbol') == BOT_STATE['symbol']]
        print(f"📊 Fills for {BOT_STATE['symbol']}: {len(symbol_fills)}")
        
        if len(symbol_fills) < 2:
            print(f"⚠️ Not enough fills for {BOT_STATE['symbol']} (need at least 2)")
            return None
        
        # Fills are ordered newest first
        exit_fill = symbol_fills[0]  # Most recent
        entry_fill = symbol_fills[1]  # Older
        
        # Check if they are opposite sides
        if entry_fill.get('side') == exit_fill.get('side'):
            return None
        
        return {
            'side': entry_fill.get('side'),
            'entry_price': float(entry_fill.get('price', 0)),
            'exit_price': float(exit_fill.get('price', 0)),
            'quantity': float(entry_fill.get('size', 0)),
            'entry_time': entry_fill.get('created_at', datetime.now().isoformat()),
            'exit_time': exit_fill.get('created_at', datetime.now().isoformat())
        }
        
    except Exception as e:
        print(f"❌ Error getting entry/exit from fills: {e}")
        return None

# ========== DECISION BOT BRIDGE ==========

def _write_exit_result(exit_type, pnl, entry_data):
    """
    Decision bot ke liye exit result likhta hai.
    Decision bot sirf READ karega, kabhi TP/SL SET nahi karega.
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        result_file = os.path.join(script_dir, "last_exit.json")
        tmp = result_file + ".tmp"
        
        payload = {
            "exit_type":   exit_type,          # "TP" ya "SL"
            "pnl":         pnl,
            "timestamp":   datetime.now().isoformat(),
            "entry_price": entry_data.get('entry_price', 0) if entry_data else 0,
            "exit_price":  entry_data.get('exit_price', 0)  if entry_data else 0,
            "side":        entry_data.get('side', '')        if entry_data else '',
            "consumed":    False   # Decision bot isko True karega jab padh le
        }
        
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, result_file)
        
        print(f"     Exit result written: {exit_type} | PnL={pnl:.5f}")
        
    except Exception as e:
        print(f"     Error writing exit result: {e}")

# ========== TRADING LOGIC ==========
def get_trading_signal():
    """Read enhanced decision signal from shared signal.json file"""
    try:
        # Get the directory of this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        signal_file = os.path.join(script_dir, "signal.json")
        
        # Check if signal file exists
        if not os.path.exists(signal_file):
            print("⚠️ Signal file not found, defaulting to BUY")
            return 'buy', {}
        
        # Read signal from file
        with open(signal_file, 'r') as f:
            signal_data = json.load(f)
        
        signal = signal_data.get('signal', 'BUY')
        timestamp = signal_data.get('timestamp', '')
        layer = signal_data.get('layer', 'UNKNOWN')
        score = signal_data.get('score', 0)
        
        # Enhanced data from decision_bot.py
        position_analysis = signal_data.get('position_analysis', {})
        decision_ready = signal_data.get('decision_ready', False)
        decision_confidence = signal_data.get('decision_confidence', 0.0)
        backtest_results = signal_data.get('backtest_results', {})
        last_trade_result = signal_data.get('last_trade_result', None)
        
        # Log enhanced decision info
        if position_analysis.get('has_position'):
            pnl = position_analysis.get('pnl', 0)
            side = position_analysis.get('side', '')
            print(f"📊 Decision: {signal} [{layer}] | Position: {side} | PnL: {pnl:.2f} | Confidence: {decision_confidence:.2f} | Ready: {decision_ready}")
        else:
            print(f"📊 Decision: {signal} [{layer}] | No Position | Confidence: {decision_confidence:.2f} | Ready: {decision_ready}")
            if last_trade_result:
                print(f"📈 Last Trade Result: {last_trade_result}")
        
        if backtest_results:
            accuracy = backtest_results.get('accuracy_percentage', 0.0)
            print(f"🎯 Backtest Accuracy: {accuracy:.1f}%")
        
        # � Return decision bot signal directly
        if signal.upper() == "BUY":
            return 'buy', signal_data
        elif signal.upper() == "SELL":
            return 'sell', signal_data
        else:
            return None, signal_data  # No default signal
            
    except Exception as e:
        print(f"❌ Error reading signal file: {e}")
        return None, {}

def calculate_next_lot():
    """Calculate next lot size based on last trade result with step-based progression"""
    global LOT_CALCULATION_LOCK
    
    if LOT_CALCULATION_LOCK:
        print(" Lot calculation already in progress - using current lot")
        return BOT_STATE['current_lot']
    
    LOT_CALCULATION_LOCK = True
    
    try:
        print(f" LOT CALCULATION (LOCKED):")
        print(f"   Last PnL: {LAST_TRADE_RESULT['profit_loss']}")
        print(f"   Current Streak: {BOT_STATE['current_streak']}")
        print(f"   Current Lot: {BOT_STATE['current_lot']}")
        print(f"   Base Lot: {BOT_STATE['base_lot']}")
        print(f"   Max Streak: {BOT_STATE['max_streak']}")
        print(f"   Processed: {LAST_TRADE_RESULT['processed']}")
        
        import math
        if BOT_STATE['current_lot'] > 0 and BOT_STATE['base_lot'] > 0:
            ratio = BOT_STATE['current_lot'] / BOT_STATE['base_lot']
            if ratio == 1:
                current_step = 1
            else:
                current_step = int(math.log2(ratio)) + 1
        else:
            current_step = 1
        
        print(f"   Current Step: {current_step}")
        print(f"   NEXT LOT CALCULATION:")
        
        if LAST_TRADE_RESULT['profit_loss'] is not None and not LAST_TRADE_RESULT['processed']:
            print(" Result not processed yet - using current lot")
            return BOT_STATE['current_lot']
        
        has_position = abs(LAST_POSITION_STATE['size']) > 0.001
        
        if LAST_TRADE_RESULT['profit_loss'] is None:
            if has_position:
                print("⚠️ Position exists but no PnL - using current lot (no change)")
                return BOT_STATE['current_lot']
            else:
                # 🔥 KEY FIX: Streak hai toh streak-based lot use karo, base nahi!
                if BOT_STATE['current_streak'] > 0:
                    streak = BOT_STATE['current_streak']
                    expected_lot = BOT_STATE['base_lot'] * (2 ** streak)
                    BOT_STATE['current_lot'] = expected_lot
                    print(f"⚠️ No PnL but streak={streak} exists → using streak lot: {expected_lot}")
                    return expected_lot
                else:
                    print("✅ No previous result, no position, no streak - using base lot")
                    BOT_STATE['current_lot'] = BOT_STATE['base_lot']
                    print(f"   📊 Current Lot set to Base Lot: {BOT_STATE['current_lot']}")
                    return BOT_STATE['base_lot']
        
        if LAST_TRADE_RESULT['profit_loss'] > 0:
            BOT_STATE['current_streak'] = 0
            BOT_STATE['current_lot'] = BOT_STATE['base_lot']
            print(f"   ✅ PROFIT - Reset to Base Lot: {BOT_STATE['base_lot']} (Step 1)")
            LAST_TRADE_RESULT['profit_loss'] = None
            LAST_TRADE_RESULT['processed'] = False
            return BOT_STATE['base_lot']
        
        else:
            new_streak = BOT_STATE['current_streak'] + 1
            
            if new_streak >= BOT_STATE['max_streak']:
                BOT_STATE['current_streak'] = 0
                BOT_STATE['current_lot'] = BOT_STATE['base_lot']
                print(f"   🚨 MAX STREAK - Reset to Base Lot: {BOT_STATE['base_lot']} (Step 1)")
                LAST_TRADE_RESULT['profit_loss'] = None
                LAST_TRADE_RESULT['processed'] = False
                return BOT_STATE['base_lot']
            else:
                BOT_STATE['current_streak'] = new_streak
                next_lot = BOT_STATE['base_lot'] * (2 ** new_streak)
                BOT_STATE['current_lot'] = next_lot
                next_step = new_streak + 1
                print(f"   ❌ LOSS - Step {next_step}: Base × 2^{new_streak} = {next_lot} (Streak: {new_streak})")
                LAST_TRADE_RESULT['profit_loss'] = None
                LAST_TRADE_RESULT['processed'] = False
                return next_lot
    
    finally:
        LOT_CALCULATION_LOCK = False
        
def place_order_with_bracket(symbol, side, size, leverage, tp_pct, sl_pct):
    print(f"\n{'='*60}")
    print(f"🎯 ORDER START: {symbol} | {side.upper()} | Size: {size}")
    print(f"   TP: {tp_pct}% | SL: {sl_pct}%")
    print(f"{'='*60}")

    try:
        # ── 1. PRODUCT CONFIG ──────────────────────────────────────
        PRODUCT_CONFIG = {
            "ADAUSD": {"id": 16614, "tick": Decimal("0.0001")},  # Fixed: 101760 → 16614
            "BTCUSD": {"id": 84,     "tick": Decimal("0.5")},
            "ETHUSD": {"id": 1320,   "tick": Decimal("0.05")},
        }

        config = PRODUCT_CONFIG.get(symbol)
        if not config:
            print(f"❌ Symbol {symbol} not in config!")
            return None

        p_id = config["id"]
        tick = config["tick"]

        # ── 2. GET MARK PRICE ──────────────────────────────────────
        ticker = make_api_request('GET', f'/tickers/{symbol}')
        if not ticker or not ticker.get('result'):
            print("❌ Ticker fetch failed")
            return None

        result     = ticker['result']
        mark_price = float(result.get('mark_price') or result.get('close'))
        print(f"📊 Mark Price: {mark_price}")

        # ── 3. TICK HELPER ─────────────────────────────────────────
        def to_tick(val):
            d = Decimal(str(val))
            return (d / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        base_dec = to_tick(mark_price)

        # ── 4. CALCULATE TP/SL ─────────────────────────────────────
        if side == 'buy':
            tp_dec = to_tick(mark_price * (1 + tp_pct / 100))
            sl_dec = to_tick(mark_price * (1 - sl_pct / 100))
        else:
            tp_dec = to_tick(mark_price * (1 - tp_pct / 100))
            sl_dec = to_tick(mark_price * (1 + sl_pct / 100))

        print(f"📐 After % calc  → TP: {tp_dec} | SL: {sl_dec}")

        # ── 5. MINIMUM = 3 TICKS ONLY (percentage respect karo) ────
        # Sirf ensure karo TP/SL entry cross na kare
        MIN_TICKS = Decimal("3")
        min_gap   = tick * MIN_TICKS

        if side == 'buy':
            if tp_dec <= base_dec:
                tp_dec = base_dec + min_gap
                print(f"⚠️  TP <= entry, min pushed to: {tp_dec}")
            if sl_dec >= base_dec:
                sl_dec = base_dec - min_gap
                print(f"⚠️  SL >= entry, min pushed to: {sl_dec}")
        else:
            if tp_dec >= base_dec:
                tp_dec = base_dec - min_gap
                print(f"⚠️  TP >= entry, min pushed to: {tp_dec}")
            if sl_dec <= base_dec:
                sl_dec = base_dec + min_gap
                print(f"⚠️  SL <= entry, min pushed to: {sl_dec}")

        tp_price = str(tp_dec)
        sl_price = str(sl_dec)

        print(f"🎯 Final TP : {tp_price}  (gap: {abs(tp_dec - base_dec):.5f})")
        print(f"🛡️  Final SL : {sl_price}  (gap: {abs(sl_dec - base_dec):.5f})")

        # ── 6. PLACE ORDER ─────────────────────────────────────────
        order_data = {
            "product_id"                    : p_id,
            "side"                          : side,
            "order_type"                    : "market_order",
            "size"                          : int(size),
            "bracket_take_profit_price"     : tp_price,
            "bracket_take_profit_order_type": "market_order",
            "bracket_stop_loss_price"       : sl_price,
            "bracket_stop_loss_order_type"  : "market_order",
            "bracket_stop_trigger_method"   : "mark_price",
        }

        print(f"\n📋 Payload:\n{json.dumps(order_data, indent=3)}")

        response = make_api_request('POST', '/orders', order_data)
        print(f"\n🔍 Raw Response:\n{json.dumps(response, indent=3)}")

        # ── 7. RESULT CHECK ────────────────────────────────────────
        if response and response.get('success') and 'result' in response:
            oid     = response['result'].get('id')
            bracket = response['result'].get('bracket_orders', [])
            print(f"\n✅ ORDER PLACED! ID: {oid}")
            if bracket:
                print(f"🎯 Bracket: {bracket}")
            else:
                # ── 8. BRACKET MISSING → MANUAL PLACE ─────────────
                print("⚠️  Bracket missing! Placing manually via /orders/bracket ...")
                product_id = p_id
                bracket_payload = {
                    "product_id": product_id,
                    "take_profit_order": {
                        "order_type": "market_order",
                        "stop_price": tp_price
                    },
                    "stop_loss_order": {
                        "order_type": "market_order",
                        "stop_price": sl_price
                    }
                }
                bracket_res = make_api_request('POST', '/orders/bracket', bracket_payload)
                print(f"🔍 Bracket Response: {json.dumps(bracket_res, indent=3)}")
        else:
            err = response.get('error') if response else 'No response'
            print(f"\n❌ ORDER FAILED! Error: {err}")

        return response

    except Exception as e:
        import traceback
        print(f"❌ Exception: {e}")
        traceback.print_exc()
        return None

# ========== BOT ENGINE ==========
def auto_trading_bot_main():
    """OPTIMIZED Main bot loop - instant execution with real-time endpoints"""
    print("🤖 Auto Trading Bot Started (OPTIMIZED)")
    
    # ✅ Set leverage ONCE at start (not every loop)
    print(f"⚡ Setting leverage: {BOT_STATE['leverage']}x")
    leverage_result = set_leverage(BOT_STATE['symbol'], BOT_STATE['leverage'])
    if not leverage_result:
        print("❌ Failed to set initial leverage, stopping bot")
        return
    
    # ✅ CHECK FOR EXISTING LIVE POSITION AT START (CRITICAL)
    print(f"\n🔍 CHECKING FOR EXISTING LIVE POSITION...")
    product_id = get_product_id(BOT_STATE['symbol'])
    if product_id:
        current_pos = check_position_realtime(product_id)
        if abs(current_pos.get('size', 0)) > 0.001:
            print(f"🚨 EXISTING POSITION FOUND: {current_pos.get('size', 0)} lots")
            print(f"📊 Entry Price: {current_pos.get('entry_price', 0)}")
            if current_pos.get('size', 0) == BOT_STATE['base_lot']:
                BOT_STATE['current_streak'] = 0
                print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (base lot - streak 0)")
            elif current_pos.get('size', 0) == BOT_STATE['base_lot'] * 2:
                BOT_STATE['current_streak'] = 1
                print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (streak 1)")
            elif current_pos.get('size', 0) == BOT_STATE['base_lot'] * 4:
                BOT_STATE['current_streak'] = 2
                print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (streak 2)")
            elif current_pos.get('size', 0) == BOT_STATE['base_lot'] * 8:
                BOT_STATE['current_streak'] = 3
                print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (streak 3)")
            else:
                # For larger lots, calculate streak from power of 2
                import math
                if current_pos.get('size', 0) >= BOT_STATE['base_lot']:
                    streak = int(math.log2(current_pos.get('size', 0) / BOT_STATE['base_lot']))
                    BOT_STATE['current_streak'] = min(streak, BOT_STATE['max_streak'] - 1)
                    print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (calculated streak: {BOT_STATE['current_streak']})")
                else:
                    BOT_STATE['current_streak'] = 0
                    print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (unknown pattern - streak 0)")
            
            # Update position state
            LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
            LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
            LAST_POSITION_STATE['entry_price'] = current_pos.get('entry_price', 0)
            
            # Wait for existing position to close first
            print("⏳ Waiting for existing position to close...")
            while BOT_STATE['running'] and abs(current_pos.get('size', 0)) > 0.001:
                time.sleep(1)
                current_pos = check_position_realtime(product_id)
                print(f"📊 Position Status: {current_pos.get('size', 0)} lots")
            
            if not BOT_STATE['running']:
                return
                
            print("✅ Existing position closed, continuing with preserved lot progression...")
        else:
            print("✅ No existing position found, starting fresh...")
            # Reset to base lot for fresh start (PRESERVE BASE LOT)
            BOT_STATE['current_lot'] = BOT_STATE['base_lot']
            BOT_STATE['current_streak'] = 0
            LAST_TRADE_RESULT['profit_loss'] = None
            LAST_TRADE_RESULT['processed'] = False
    
    while BOT_STATE['running']:
        try:
            # Check for force stop
            if BOT_STATE['force_stop']:
                print("🛑 Force Stop triggered - Stopping bot immediately!")
                BOT_STATE['running'] = False
                BOT_STATE['force_stop'] = False
                break
            
            print(f"\n{'='*50}")
            print(f"🔍 BOT LOOP CHECK - Symbol: {BOT_STATE['symbol']}")
            print(f"📊 Running: {BOT_STATE['running']}, Streak: {BOT_STATE['current_streak']}")
            print(f"💰 Current Lot: {BOT_STATE['current_lot']}")
            print(f"{'='*50}")
            
            # Check position and detect closure (REAL-TIME)
            has_position, was_closed, pnl = check_position_and_detect_closure()
            
            if was_closed:
                print(f"🎯 Position was closed! PnL: {pnl}")
                print(f"📊 Result: {'PROFIT ✅' if pnl > 0 else 'LOSS ❌'}")
                print(f"💰 Next lot will be calculated based on this result")
                
                # ✅ STOP AT WIN LOGIC (NO SPEED IMPACT)
                if pnl > 0 and BOT_STATE['stop_at_win']:
                    print(f"🏆 PROFIT ACHIEVED - STOP AT WIN ACTIVATED!")
                    print(f"🛑 Bot will stop placing new trades...")
                    BOT_STATE['running'] = False
                    BOT_STATE['stop_at_win'] = False  # Reset flag
                    continue  # Skip to next iteration (will exit loop)
                
                # ✅ STOP AT MAX STREAK LOGIC (NEW)
                # Check if this loss will reach max streak (current_streak + 1 >= max_streak)
                if pnl < 0 and (BOT_STATE['current_streak'] + 1) >= BOT_STATE['max_streak'] and BOT_STATE['stop_at_max_streak']:
                    print(f"🚨 MAX LOSS STREAK HIT! - STOP AT MAX STREAK ACTIVATED!")
                    print(f"📊 Current streak: {BOT_STATE['current_streak']}, Max streak: {BOT_STATE['max_streak']}")
                    print(f"🛑 Bot will stop placing new trades...")
                    BOT_STATE['running'] = False
                    BOT_STATE['stop_at_max_streak'] = False  # Reset flag
                    continue  # Skip to next iteration (will exit loop)
            
            if has_position:
                print("⏳ Active position found, waiting for closure...")
                time.sleep(0.5)  # ✅ Faster check - 0.5 seconds
                continue
            
            # ✅ CHECK STOP CONDITIONS BEFORE PLACING ORDER (NO DELAY)
            if BOT_STATE['force_stop']:
                print("🛑 FORCE STOP ACTIVE - Skipping order placement")
                BOT_STATE['running'] = False
                continue
            
            if BOT_STATE['stop_at_win']:
                print("🎯 STOP AT WIN ACTIVE - Will stop after next profit")
            
            if BOT_STATE['stop_at_max_streak']:
                print(f"⚠️ STOP AT MAX STREAK ACTIVE - Will stop at {BOT_STATE['max_streak']} losses in a row")
            
            # 🔥 FIX 3: MOST IMPORTANT - Don't trade until result processed
            if LAST_TRADE_RESULT['profit_loss'] is not None and not LAST_TRADE_RESULT['processed']:
                print("⚠️ RESULT NOT PROCESSED - WAITING...")
                
                # 🆕 AUTOMATIC STUCK DETECTION & CLEARING
                # Check if result is stuck for too long (more than 30 seconds)
                if LAST_TRADE_RESULT['timestamp']:
                    from datetime import datetime, timedelta
                    result_time = datetime.fromisoformat(LAST_TRADE_RESULT['timestamp'])
                    time_stuck = datetime.now() - result_time
                    
                    if time_stuck.total_seconds() > 30:  # Stuck for more than 30 seconds
                        print(f"🚨 RESULT STUCK for {time_stuck.total_seconds():.0f} seconds - AUTO CLEARING...")
                        clear_stuck_trade_result()
                        continue  # Continue to next iteration with cleared result
                
                continue
            
            # No active position - calculate next lot
            next_lot = calculate_next_lot()
            print(f"💰 Next Lot Size: {next_lot}")
            print(f"🎯 ORDER DETAILS:")
            print(f"   📊 Base Lot: {BOT_STATE['base_lot']}")
            print(f"   📊 Current Lot: {BOT_STATE['current_lot']}")
            print(f"   📊 Next Lot: {next_lot}")
            print(f"   📊 Current Streak: {BOT_STATE['current_streak']}")
            
            # ✅ Reset processed flag before placing new order
            LAST_TRADE_RESULT['processed'] = False
            
            # Determine side using enhanced trading signal logic
            signal_result = get_trading_signal()
            side = signal_result[0] if signal_result else 'buy'
            signal_data = signal_result[1] if signal_result and len(signal_result) > 1 else {}
            
            # Skip if decision not ready
            if side is None:
                print("⏳ Waiting for bot decision - skipping this cycle")
                time.sleep(1)
                continue
            
            print(f"📈 Trading Signal: {side.upper()} order with {next_lot} lots")
            
            # Enhanced logging with position analysis
            if signal_data:
                position_analysis = signal_data.get('position_analysis', {})
                if position_analysis.get('tp_value') and position_analysis.get('sl_value'):
                    print(f"🎯 TP/SL Values: TP={position_analysis.get('tp_value'):.2f} | SL={position_analysis.get('sl_value'):.2f}")
            
            # ✅ FINAL POSITION CHECK BEFORE ORDER (CRITICAL SAFETY)
            product_id = get_product_id(BOT_STATE['symbol'])
            if product_id:
                # Double-check position status with real-time endpoint
                current_pos = check_position_realtime(product_id)
                if abs(current_pos.get('size', 0)) > 0.001:
                    print("⛔ SAFETY CHECK: Real position exists - SKIPPING ORDER")
                    print(f"📊 Position Size: {current_pos.get('size', 0)} lots")
                    print(f"📊 Entry Price: {current_pos.get('entry_price', 0)}")
                    
                    # Update our state to match reality
                    LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
                    LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
                    if abs(LAST_POSITION_STATE['size']) > 0.001:
                        print("⏳ Active position found, waiting for closure...")
                        continue
                    else:
                        print("✅ SAFETY CHECK: No active position - safe to proceed")
                        
                        # Additional safety: Check for API failures that might cause false closures
                        if LAST_TRADE_RESULT['profit_loss'] is None and BOT_STATE['current_lot'] != BOT_STATE['base_lot']:
                            print("⚠️ Ignoring false closure due to network issue - keeping current lot")
                            continue  # Skip lot calculation if API failed
            
            # Place order with bracket
            print(f"🎯 PLACING ORDER WITH CALCULATED LOT: {next_lot}")
            print(f"🎯 BOT STATE BEFORE ORDER:")
            print(f"   📊 Base Lot: {BOT_STATE['base_lot']}")
            print(f"   📊 Current Lot: {BOT_STATE['current_lot']}")
            print(f"   📊 Next Lot: {next_lot}")
            
            order_response = place_order_with_bracket(
                BOT_STATE['symbol'], 
                side, 
                next_lot, 
                BOT_STATE['leverage'], 
                BOT_STATE['tp_percent'], 
                BOT_STATE['sl_percent']
            )
            
            if order_response and order_response.get('success'):
                print(f"✅ Order placed successfully!")
                print(f"📋 Order ID: {order_response.get('result', {}).get('id')}")
                
                # ✅ MINIMAL wait for position confirmation (TP/SL already attached via bracket)
                print("⚡ Quick position check (TP/SL already attached)...")
                max_wait_time = 2  # Reduced from 5s since TP/SL is instant
                wait_start = time.time()
                
                while time.time() - wait_start < max_wait_time:
                    time.sleep(0.05)  # Check every 50ms for faster response
                    current_pos = check_position_realtime(product_id)
                    if abs(current_pos.get('size', 0)) > 0.001:
                        print(f"⚡ Position confirmed: {current_pos.get('size', 0)} lots (TP/SL active)")
                        break
                else:
                    print("⚠️ Position check timeout - continuing (TP/SL should be active)")
                
                # ✅ Use REAL-TIME endpoint to update position state (with validation)
                product_id = get_product_id(BOT_STATE['symbol'])
                if product_id:
                    current_pos = check_position_realtime(product_id)
                    
                    # Check if API failed
                    if current_pos.get('error'):
                        print("⚠️ API FAILED - SKIPPING POSITION UPDATE")
                        continue  # Skip this loop iteration
                    
                    if current_pos.get('has_position') and abs(current_pos.get('size', 0)) > 0.001:  # Only update if position exists
                        LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
                        LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
                        LAST_POSITION_STATE['entry_price'] = current_pos.get('entry_price', 0)
                        
                        # IMPORTANT: Sync current_lot with actual position size
                        BOT_STATE['current_lot'] = current_pos.get('size', 0)
                        
                        print(f"📊 Position State Updated: Size={LAST_POSITION_STATE['size']}, Entry={LAST_POSITION_STATE['entry_price']}")
                        print(f"🔄 Current Lot Synced: {BOT_STATE['current_lot']} = {current_pos.get('size', 0)} (from actual position)")
                    else:
                        print("⚠️ Position not found in real-time check")
                else:
                    print("❌ Could not get product_id for position update")
            else:
                print("❌ Order failed!")
                print(f"📋 Order Response: {order_response}")
                time.sleep(0.5)  # ✅ Faster retry - 0.5 seconds
            
        except Exception as e:
            # THIS IS THE KEY: If any error happens, the bot doesn't stop.
            # It prints the error and restarts the loop after a short safety delay.
            print(f"🚨 TEMPORARY BOT ERROR: {e}")
            import traceback
            traceback.print_exc()
            print("🔄 Bot is staying alive... retrying in 5 seconds")
            time.sleep(5) 
            continue # Restarts the 'while' loop immediately
    
    print("🤖 Auto Trading Bot Stopped")

def start_auto_trading_bot():
    """Start the bot"""
    if BOT_STATE['running']:
        return False
    
    # Initialize position state
    global LAST_POSITION_STATE
    LAST_POSITION_STATE = {
        'symbol': BOT_STATE['symbol'],
        'size': 0,
        'entry_price': 0
    }
    
    # Reset stop flags
    BOT_STATE['stop_at_win'] = False
    BOT_STATE['stop_at_max_streak'] = False
    BOT_STATE['force_stop'] = False
    
    # Reset session P&L for fresh start
    from datetime import datetime
    BOT_STATE['session_start_time'] = datetime.now().isoformat()
    BOT_STATE['session_total_pnl'] = 0.0
    print(f"Session started at {BOT_STATE['session_start_time']}")
    
    BOT_STATE['running'] = True
    BOT_STATE['thread'] = threading.Thread(target=auto_trading_bot_main, daemon=True)
    BOT_STATE['thread'].start()
    return True

def stop_auto_trading_bot():
    """Stop the bot"""
    BOT_STATE['running'] = False
    if BOT_STATE['thread']:
        BOT_STATE['thread'].join(timeout=5)
    
    # Clear session when bot is stopped
    if BOT_STATE['session_start_time']:
        print(f"Session ended. Final P&L: ${BOT_STATE['session_total_pnl']:.2f}")
        BOT_STATE['session_start_time'] = None
        BOT_STATE['session_total_pnl'] = 0.0
    
    return True

def clear_stuck_trade_result():
    """Clear stuck trade result to fix bot"""
    global LAST_TRADE_RESULT
    
    print("🔧 CLEARING STUCK TRADE RESULT...")
    print(f"   Previous result: P&L={LAST_TRADE_RESULT['profit_loss']}, Processed={LAST_TRADE_RESULT['processed']}")
    
    # Reset the trade result
    LAST_TRADE_RESULT = {
        'profit_loss': None,
        'timestamp': None,
        'lot_used': None,
        'processed': False
    }
    
    print("   ✅ Trade result cleared - Bot can continue")
    return True

def reconcile_stuck_trades_from_database():
    """Check MySQL database for trades that might be stuck and auto-clear them"""
    global LAST_TRADE_RESULT
    
    try:
        # Get the most recent trades that might be stuck
        query = '''
            SELECT symbol, side, entry_price, exit_price, quantity, pnl, entry_time, exit_time, created_at
            FROM closed_positions 
            WHERE exit_time IS NOT NULL
            ORDER BY created_at DESC 
            LIMIT 3
        '''
        
        recent_trades = execute_mysql_query(query, fetch_all=True)
        
        if not recent_trades:
            print("   ℹ️ No recent trades found in MySQL database")
            return
        
        # Check if we have a stuck result that matches recent database trades
        if (LAST_TRADE_RESULT['profit_loss'] is not None and 
            not LAST_TRADE_RESULT['processed']):
            
            print(f"   🔍 Found {len(recent_trades)} recent trades in MySQL database")
            
            # Check if the stuck result matches any recent trade
            for trade in recent_trades:
                db_pnl = trade['pnl']  # Using dict cursor
                if abs(float(db_pnl) - LAST_TRADE_RESULT['profit_loss']) < 0.01:
                    print(f"   🎯 Found matching trade in MySQL DB: P&L={db_pnl}")
                    print("   🆕 AUTO-CLEARING stuck result based on database match")
                    clear_stuck_trade_result()
                    return
            
            # If no match found, clear anyway to unstick bot
            print("   ⚠️ No exact match found - AUTO-CLEARING to unstick bot")
            clear_stuck_trade_result()
        else:
            print("   ✅ No stuck result detected")
            
    except Exception as e:
        print(f"   ❌ Error in database reconciliation: {e}")
        # On error, clear to be safe
        clear_stuck_trade_result()


# ========== API ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    """Health check endpoint for keepalive services"""
    return jsonify({
        'status': 'alive',
        'timestamp': datetime.now().isoformat(),
        'service': 'delta-trading-bot',
        'uptime': 'running'
    })

@app.route('/api/system-ip', methods=['GET'])
def get_system_ip():
    """Get current system IP address"""
    try:
        import socket
        import requests
        
        # Get public IP
        try:
            public_ip = requests.get('https://ipinfo.io/ip', timeout=5).text.strip()
        except:
            public_ip = "Unknown"
        
        # Get local IP
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
        except:
            local_ip = "Unknown"
        
        return jsonify({
            'success': True,
            'public_ip': public_ip,
            'local_ip': local_ip,
            'port': 8090
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error getting IP: {str(e)}'}), 500

@app.route('/api/start-bot', methods=['POST'])
def start_bot():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        # Input validation
        base_lot = data.get('base_lot', 1)
        leverage = data.get('leverage', 10)
        tp_percent = data.get('tp_percent', 2.0)
        sl_percent = data.get('sl_percent', 1.0)
        max_streak = data.get('max_streak', 5)
        symbol = data.get('symbol', 'ADAUSD')
        
        # Validate inputs
        if not isinstance(base_lot, int) or base_lot < 1 or base_lot > 10000:
            return jsonify({'success': False, 'message': 'Base lot must be integer between 1-10000'}), 400
        
        if not isinstance(leverage, int) or leverage < 1 or leverage > 200:
            return jsonify({'success': False, 'message': 'Leverage must be integer between 1-200'}), 400
        
        if not isinstance(tp_percent, (int, float)) or tp_percent < 0.1 or tp_percent > 50:
            return jsonify({'success': False, 'message': 'TP percent must be between 0.1-50'}), 400
        
        if not isinstance(sl_percent, (int, float)) or sl_percent < 0.1 or sl_percent > 50:
            return jsonify({'success': False, 'message': 'SL percent must be between 0.1-50'}), 400
        
        if not isinstance(max_streak, int) or max_streak < 1 or max_streak > 20:
            return jsonify({'success': False, 'message': 'Max streak must be integer between 1-20'}), 400
        
        if not isinstance(symbol, str) or len(symbol) < 1 or len(symbol) > 20:
            return jsonify({'success': False, 'message': 'Symbol must be string between 1-20 characters'}), 400
        
        # Update bot settings with validated data
        # ✅ ONLY UPDATE base_lot if bot is NOT running (prevent changes during active trading)
        if not BOT_STATE['running'] and 'base_lot' in data and data['base_lot'] != BOT_STATE.get('base_lot', 1):
            BOT_STATE['base_lot'] = base_lot
            BOT_STATE['current_lot'] = base_lot  # Also reset current lot to new base lot
            print(f"✅ Base lot updated to: {base_lot}")
            print(f"✅ Current lot reset to new base lot: {base_lot}")
        elif BOT_STATE['running'] and 'base_lot' in data:
            print(f"⚠️ Bot is running - cannot change base lot. Stop bot first to change base lot.")
        
        BOT_STATE['leverage'] = leverage
        BOT_STATE['tp_percent'] = float(tp_percent)
        BOT_STATE['sl_percent'] = float(sl_percent)
        BOT_STATE['max_streak'] = max_streak
        BOT_STATE['symbol'] = symbol
        
        print(f"🎯 BOT STARTING WITH:")
        print(f"   📊 Base Lot: {BOT_STATE['base_lot']}")
        print(f"   📊 Current Lot: {BOT_STATE['current_lot']}")
        print(f"   📊 Current Streak: {BOT_STATE['current_streak']}")
        print(f"   📊 Symbol: {BOT_STATE['symbol']}")
        
        # Reset trade result for fresh start
        BOT_STATE['current_streak'] = 0
        LAST_TRADE_RESULT['profit_loss'] = None
        LAST_TRADE_RESULT['processed'] = False
        
        # IMPORTANT: Reset current_lot to base_lot when starting fresh
        BOT_STATE['current_lot'] = BOT_STATE['base_lot']
        print(f"✅ Fresh start - current_lot reset to base_lot: {BOT_STATE['current_lot']}")
        print(f"✅ Fresh start - streak reset to 0")
        
        BOT_STATE['symbol'] = symbol.upper()
        
        if start_auto_trading_bot():
            return jsonify({'success': True, 'message': 'Bot started successfully'})
        return jsonify({'success': False, 'message': 'Bot already running'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Invalid request: {str(e)}'}), 400

@app.route('/api/force-stop', methods=['POST'])
def force_stop_bot():
    """Force stop bot immediately"""
    try:
        BOT_STATE['force_stop'] = True
        print("🛑 FORCE STOP ACTIVATED - Bot will stop immediately")
        return jsonify({'success': True, 'message': 'Force stop activated'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/stop-at-win', methods=['POST'])
def stop_at_win():
    """Stop bot after next profitable trade"""
    try:
        BOT_STATE['stop_at_win'] = True
        BOT_STATE['force_stop'] = False  # Clear force stop if set
        BOT_STATE['stop_at_max_streak'] = False  # Clear max streak stop if set
        print("🎯 STOP AT WIN ACTIVATED - Bot will stop after next profit")
        return jsonify({'success': True, 'message': 'Stop at win activated'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/stop-at-max-streak', methods=['POST'])
def stop_at_max_streak():
    """Stop bot when max loss streak is hit"""
    try:
        BOT_STATE['stop_at_max_streak'] = True
        BOT_STATE['force_stop'] = False  # Clear force stop if set
        BOT_STATE['stop_at_win'] = False  # Clear stop at win if set
        print(f"⚠️ STOP AT MAX STREAK ACTIVATED - Bot will stop at {BOT_STATE['max_streak']} losses in a row")
        return jsonify({'success': True, 'message': f"Stop at max streak ({BOT_STATE['max_streak']}) activated"})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/clear-stop-conditions', methods=['POST'])
def clear_stop_conditions():
    """Clear all stop conditions"""
    try:
        BOT_STATE['stop_at_win'] = False
        BOT_STATE['stop_at_max_streak'] = False
        BOT_STATE['force_stop'] = False
        print("✅ STOP CONDITIONS CLEARED - Bot will run normally")
        return jsonify({'success': True, 'message': 'Stop conditions cleared'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/update-symbol', methods=['POST'])
def update_symbol():
    """Update trading symbol for bot and decision bot"""
    try:
        data = request.get_json()
        new_symbol = data.get('symbol')
        
        if not new_symbol:
            return jsonify({'success': False, 'message': 'Symbol is required'}), 400
        
        # Validate symbol
        valid_symbols = ['ADAUSD']
        if new_symbol not in valid_symbols:
            return jsonify({'success': False, 'message': f'Invalid symbol. Valid symbols: {valid_symbols}'}), 400
        
        old_symbol = BOT_STATE['symbol']
        BOT_STATE['symbol'] = new_symbol
        
        print(f"📊 Trading symbol updated: {old_symbol} → {new_symbol}")
        return jsonify({'success': True, 'message': f'Symbol updated to {new_symbol}'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error updating symbol: {str(e)}'}), 500

@app.route('/api/stop-bot', methods=['POST'])
def stop_bot():
    try:
        if stop_auto_trading_bot():
            return jsonify({'success': True, 'message': 'Bot stopped successfully'})
        return jsonify({'success': False, 'message': 'Bot was not running'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error stopping bot: {str(e)}'}), 500

@app.route('/api/bot-status', methods=['GET'])
def get_bot_status():
    try:
        # Calculate next lot if loss
        next_lot_if_loss = BOT_STATE['current_lot'] * 2
        if BOT_STATE['current_streak'] + 1 >= BOT_STATE['max_streak']:
            next_lot_if_loss = BOT_STATE['base_lot']  # Reset to base if max streak reached
        
        # Get current market lot size and calculate step
        current_lot_size = LOT_SIZES.get(BOT_STATE['symbol'], 10)
        next_lot_quantity = next_lot_if_loss * current_lot_size
        
        # Calculate current step from current lot and base lot
        import math
        if BOT_STATE['current_lot'] > 0 and BOT_STATE['base_lot'] > 0:
            # Fix: Base lot should be Step 1, not Step 0
            ratio = BOT_STATE['current_lot'] / BOT_STATE['base_lot']
            if ratio == 1:
                current_step = 1  # Base lot = Step 1
            else:
                current_step = int(math.log2(ratio)) + 1
        else:
            current_step = 1
        
        return jsonify({
            'success': True,
            'status': {
                'running': BOT_STATE['running'],
                'current_lot': BOT_STATE['current_lot'],
                'current_streak': BOT_STATE['current_streak'],
                'last_result': BOT_STATE['last_result'],
                'base_lot': BOT_STATE['base_lot'],
                'leverage': BOT_STATE['leverage'],
                'tp_percent': BOT_STATE['tp_percent'],
                'sl_percent': BOT_STATE['sl_percent'],
                'max_streak': BOT_STATE['max_streak'],
                'symbol': BOT_STATE['symbol'],
                'stop_at_win': BOT_STATE['stop_at_win'],
                'stop_at_max_streak': BOT_STATE['stop_at_max_streak'],
                'force_stop': BOT_STATE['force_stop'],
                'next_lot_if_loss': next_lot_if_loss,
                'current_lot_size': current_lot_size,
                'next_lot_quantity': next_lot_quantity,
                'current_step': current_step,
                'session_start_time': BOT_STATE['session_start_time'],
                'session_total_pnl': BOT_STATE['session_total_pnl']
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error getting status: {str(e)}'}), 500

@app.route('/api/force-stop', methods=['POST'])
def force_stop():
    try:
        if not BOT_STATE['running']:
            return jsonify({'success': False, 'message': 'Bot is not running'})
        
        BOT_STATE['force_stop'] = True
        print("🛑 Force stop activated - Bot will stop immediately")
        return jsonify({'success': True, 'message': 'Force stop initiated'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error force stopping: {str(e)}'}), 500

@app.route('/api/clear-stuck-result', methods=['POST'])
def clear_stuck_result():
    """Clear stuck trade result to fix bot"""
    try:
        if clear_stuck_trade_result():
            return jsonify({'success': True, 'message': 'Stuck trade result cleared'})
        else:
            return jsonify({'success': False, 'message': 'Failed to clear stuck result'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error clearing stuck result: {str(e)}'}), 500

@app.route('/api/bot-status', methods=['GET'])
def bot_status():
    return jsonify({
        'running': BOT_STATE['running'],
        'current_lot': BOT_STATE['current_lot'],
        'current_streak': BOT_STATE['current_streak'],
        'last_result': BOT_STATE['last_result'],
        'base_lot': BOT_STATE['base_lot'],
        'leverage': BOT_STATE['leverage'],
        'tp_percent': BOT_STATE['tp_percent'],
        'sl_percent': BOT_STATE['sl_percent'],
        'max_streak': BOT_STATE['max_streak'],
        'symbol': BOT_STATE['symbol'],
        'stop_at_win': BOT_STATE['stop_at_win'],
        'stop_at_max_streak': BOT_STATE['stop_at_max_streak'],
        'force_stop': BOT_STATE['force_stop']
    })




@app.route('/api/wallet-balance', methods=['GET'])
def wallet_balance():
    balance_data = get_wallet_balance()
    return jsonify(balance_data)

@app.route('/api/trade-history', methods=['GET'])
def trade_history():
    # Get pagination parameters
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page
    
    try:
        # Get total count for pagination
        count_result = execute_mysql_query('SELECT COUNT(*) as total FROM closed_positions', fetch_one=True)
        total_trades = count_result['total'] if count_result else 0
        
        # Get trades for current page
        query = '''
            SELECT id, symbol, side, entry_price, exit_price, quantity, pnl, entry_time, exit_time
            FROM closed_positions 
            ORDER BY created_at DESC 
            LIMIT %s OFFSET %s
        '''
        trades = execute_mysql_query(query, (per_page, offset), fetch_all=True)
        
        return jsonify({
            'trades': [{
                'symbol': t['symbol'],
                'side': t['side'], 
                'entry_price': float(t['entry_price']) if t['entry_price'] else None,
                'exit_price': float(t['exit_price']) if t['exit_price'] else None,
                'quantity': float(t['quantity']) if t['quantity'] else None,
                'pnl': float(t['pnl']) if t['pnl'] else None,
                'entry_time': t['entry_time'],
                'exit_time': t['exit_time'],
                'id': str(t['id']) if t.get('id') else f"trade_{hash(t['entry_time'] + t['symbol'])}"
            } for t in trades],
            'pagination': {
                'current_page': page,
                'per_page': per_page,
                'total': total_trades,
                'total_pages': (total_trades + per_page - 1) // per_page,
                'has_next': page * per_page < total_trades,
                'has_prev': page > 1
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'trades': [], 'pagination': None})

@app.route('/api/delete-trades', methods=['POST'])
def delete_trades():
    try:
        data = request.get_json()
        trade_ids = data.get('trade_ids', [])
        
        if not trade_ids:
            return jsonify({'success': False, 'message': 'No trade IDs provided'})
        
        # Convert string IDs to integers if they're numeric
        numeric_ids = []
        for trade_id in trade_ids:
            try:
                # Handle both numeric IDs and hash-based IDs
                if trade_id.startswith('trade_'):
                    continue  # Skip hash-based IDs for now
                numeric_ids.append(int(trade_id))
            except ValueError:
                continue
        
        if not numeric_ids:
            return jsonify({'success': False, 'message': 'No valid trade IDs found'})
        
        # Build placeholders for IN clause
        placeholders = ','.join(['%s'] * len(numeric_ids))
        
        # Delete trades
        delete_query = f'DELETE FROM closed_positions WHERE id IN ({placeholders})'
        execute_mysql_query(delete_query, numeric_ids, commit=True)
        
        return jsonify({
            'success': True, 
            'message': f'Successfully deleted {len(numeric_ids)} trade(s)'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error deleting trades: {str(e)}'})


# # ===== INDEPENDENT TP/SL GUARDIAN (ONLY EDIT & ADD - NO DELETE) =====
# def auto_tp_sl_guardian():
#     """
#     🛡️ SAFE TP/SL GUARDIAN
#     - Runs every 2 seconds
#     - Edits wrong TP/SL (No Deletion)
#     - Places missing TP/SL immediately
#     - Uses Tolerance to avoid constant editing
#     """
#     print("🛡️ SAFE TP/SL GUARDIAN STARTED (EDIT ONLY)...")

#     while True:
#         try:
#             time.sleep(2)

#             print(f"\n{'='*80}")
#             print(f"🛡️ GUARDIAN CHECK - {datetime.now().strftime('%H:%M:%S')}")
#             print(f"{'='*80}")

#             positions_response = make_api_request('GET', '/positions/margined')
#             if not positions_response or not positions_response.get('success'):
#                 continue

#             active_positions = [
#                 p for p in positions_response.get('result', [])
#                 if abs(float(p.get('size', 0))) > 0.0001
#             ]

#             if not active_positions:
#                 print("ℹ️ No active positions")
#                 continue

#             for pos in active_positions:
#                 try:
#                     symbol = pos.get("product_symbol") or pos.get("symbol")
#                     size = float(pos.get("size", 0))
#                     entry = float(pos.get("entry_price", 0))
#                     product_id = pos.get("product_id")

#                     if not all([symbol, product_id]) or abs(size) < 0.0001 or entry <= 0:
#                         continue

#                     print(f"\n📍 [GUARDIAN] {symbol} | Size: {size} | Entry: {entry}")

#                     # ===== CALCULATE EXPECTED TP/SL =====
#                     if size > 0:  # LONG
#                         expected_tp = entry * (1 + LIVE_TP_PERCENTAGE / 100)
#                         expected_sl = entry * (1 - LIVE_SL_PERCENTAGE / 100)
#                     else:  # SHORT
#                         expected_tp = entry * (1 - LIVE_TP_PERCENTAGE / 100)
#                         expected_sl = entry * (1 + LIVE_SL_PERCENTAGE / 100)

#                     # 🔥 DYNAMIC TOLERANCE: 0.05% gap allow karein taaki baar-baar edit na ho
#                     dynamic_tolerance = entry * 0.0005 
#                     print(f"   💡 Expected TP: {expected_tp:.6f} | Expected SL: {expected_sl:.6f} | Tolerance: {dynamic_tolerance:.6f}")

#                     # ===== GET EXISTING ORDERS =====
#                     orders_response = make_api_request('GET', f'/orders?product_id={product_id}&state=open')
#                     if not orders_response or not orders_response.get('success'):
#                         continue
                    
#                     orders = orders_response.get("result", [])
#                     tp_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "take_profit_order"]
#                     sl_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "stop_loss_order"]

#                     # ===== VALIDATE TP/SL WITH TOLERANCE =====
#                     tp_valid = False
#                     sl_valid = False
#                     wrong_tp_orders = []
#                     wrong_sl_orders = []

#                     for tp_order in tp_orders:
#                         stop_price = float(tp_order.get("stop_price", 0))
#                         if abs(stop_price - expected_tp) < dynamic_tolerance:
#                             tp_valid = True
#                             print(f"   ✅ TP Order {tp_order.get('id')} is CORRECT")
#                         else:
#                             wrong_tp_orders.append(tp_order)

#                     for sl_order in sl_orders:
#                         stop_price = float(sl_order.get("stop_price", 0))
#                         if abs(stop_price - expected_sl) < dynamic_tolerance:
#                             sl_valid = True
#                             print(f"   ✅ SL Order {sl_order.get('id')} is CORRECT")
#                         else:
#                             wrong_sl_orders.append(sl_order)

#                     # ===== STEP 1: EDIT WRONG ORDERS ONLY =====
#                     tp_edited = False
#                     sl_edited = False
                    
#                     if wrong_tp_orders and not tp_valid:
#                         for tp_order in wrong_tp_orders:
#                             order_id = tp_order.get("id")
#                             print(f"   🔧 EDITING TP order {order_id}...")
#                             edit_payload = {"id": order_id, "product_id": int(product_id), "order_type": "market_order", "stop_price": "{:.6f}".format(expected_tp), "size": abs(int(size))}
#                             edit_body = json.dumps(edit_payload)
#                             try:
#                                 edit_res = requests.put(BASE_URL + "/v2/orders", headers=sign_request("PUT", "/v2/orders", edit_body), data=edit_body, timeout=10)
#                                 if edit_res.status_code == 200:
#                                     print(f"   ✅ TP EDITED successfully")
#                                     tp_edited = True
#                                     break
#                             except: pass
                    
#                     if wrong_sl_orders and not sl_valid:
#                         for sl_order in wrong_sl_orders:
#                             order_id = sl_order.get("id")
#                             print(f"   🔧 EDITING SL order {order_id}...")
#                             edit_payload = {"id": order_id, "product_id": int(product_id), "order_type": "market_order", "stop_price": "{:.6f}".format(expected_sl), "size": abs(int(size))}
#                             edit_body = json.dumps(edit_payload)
#                             try:
#                                 edit_res = requests.put(BASE_URL + "/v2/orders", headers=sign_request("PUT", "/v2/orders", edit_body), data=edit_body, timeout=10)
#                                 if edit_res.status_code == 200:
#                                     print(f"   ✅ SL EDITED successfully")
#                                     sl_edited = True
#                                     break
#                             except: pass

#                     # ===== STEP 2: PLACE MISSING TP/SL (ONLY IF NOT EXISTS) =====
#                     need_tp = not tp_valid and not tp_edited
#                     need_sl = not sl_valid and not sl_edited
                    
#                     if need_tp or need_sl:
#                         # 🔥 SAFETY CHECK: Price check before adding new bracket
#                         ticker = make_api_request('GET', f'/tickers/{symbol}')
#                         if ticker:
#                             curr_price = float(ticker['result']['close'])
#                             # Check if price already hit or too close
#                             is_safe = True
#                             if size > 0: # Long
#                                 if expected_tp <= curr_price or expected_sl >= curr_price: is_safe = False
#                             else: # Short
#                                 if expected_tp >= curr_price or expected_sl <= curr_price: is_safe = False
                            
#                             if not is_safe:
#                                 print(f"   ⚠️ Price too close to TP/SL. Skipping placement to avoid error.")
#                                 continue

#                         print(f"   📤 Placing missing TP/SL...")
#                         payload = {
#                             "product_id": int(product_id),
#                             "take_profit_order": {"order_type": "market_order", "stop_price": "{:.6f}".format(expected_tp)},
#                             "stop_loss_order": {"order_type": "market_order", "stop_price": "{:.6f}".format(expected_sl)}
#                         }
#                         body = json.dumps(payload)
#                         try:
#                             res = requests.post(BASE_URL + "/v2/orders/bracket", headers=sign_request("POST", "/v2/orders/bracket", body), data=body, timeout=10)
#                             if res.status_code == 200:
#                                 print(f"   ✅ Bracket placed successfully")
#                         except: pass
                    
#                     time.sleep(0.3)

#                 except Exception as e:
#                     print(f"   ❌ Error: {e}")

#         except Exception as e:
#             print(f"❌ Guardian error: {e}")
#             time.sleep(2)


# ===== INDEPENDENT TP/SL GUARDIAN (ONLY EDIT & ADD - NO DELETE) =====
def auto_tp_sl_guardian():
    """
    🛡️ SAFE TP/SL GUARDIAN
    - Runs every 2 seconds
    - Edits wrong TP/SL (No Deletion)
    - Places missing TP/SL immediately
    - Uses Tolerance to avoid constant editing
    """
    print("🛡️ SAFE TP/SL GUARDIAN STARTED (EDIT ONLY)...")

    while True:
        try:
            time.sleep(2)

            print(f"\n{'='*80}")
            print(f"🛡️ GUARDIAN CHECK - {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*80}")

            positions_response = make_api_request('GET', '/positions/margined')
            if not positions_response or not positions_response.get('success'):
                continue

            active_positions = [
                p for p in positions_response.get('result', [])
                if abs(float(p.get('size', 0))) > 0.0001
            ]

            if not active_positions:
                print("ℹ️ No active positions")
                continue

            for pos in active_positions:
                try:
                    symbol = pos.get("product_symbol") or pos.get("symbol")
                    size = float(pos.get("size", 0))
                    entry = float(pos.get("entry_price", 0))
                    product_id = pos.get("product_id")

                    if not all([symbol, product_id]) or abs(size) < 0.0001 or entry <= 0:
                        continue

                    print(f"\n📍 [GUARDIAN] {symbol} | Size: {size} | Entry: {entry}")

                    # ===== CALCULATE EXPECTED TP/SL =====
                    if size > 0:  # LONG
                        expected_tp = entry * (1 + LIVE_TP_PERCENTAGE / 100)
                        expected_sl = entry * (1 - LIVE_SL_PERCENTAGE / 100)
                    else:  # SHORT
                        expected_tp = entry * (1 - LIVE_TP_PERCENTAGE / 100)
                        expected_sl = entry * (1 + LIVE_SL_PERCENTAGE / 100)

                    # 🔥 DYNAMIC TOLERANCE: 0.05% gap allow karein taaki baar-baar edit na ho
                    dynamic_tolerance = entry * 0.0005 
                    print(f"   💡 Expected TP: {expected_tp:.6f} | Expected SL: {expected_sl:.6f} | Tolerance: {dynamic_tolerance:.6f}")

                    # ===== GET EXISTING ORDERS =====
                    orders_response = make_api_request('GET', f'/orders?product_id={product_id}&state=open')
                    if not orders_response or not orders_response.get('success'):
                        continue
                    
                    orders = orders_response.get("result", [])
                    tp_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "take_profit_order"]
                    sl_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "stop_loss_order"]

                    # ===== VALIDATE TP/SL WITH TOLERANCE =====
                    tp_valid = False
                    sl_valid = False
                    wrong_tp_orders = []
                    wrong_sl_orders = []

                    for tp_order in tp_orders:
                        stop_price = float(tp_order.get("stop_price", 0))
                        if abs(stop_price - expected_tp) < dynamic_tolerance:
                            tp_valid = True
                            print(f"   ✅ TP Order {tp_order.get('id')} is CORRECT")
                        else:
                            wrong_tp_orders.append(tp_order)

                    for sl_order in sl_orders:
                        stop_price = float(sl_order.get("stop_price", 0))
                        if abs(stop_price - expected_sl) < dynamic_tolerance:
                            sl_valid = True
                            print(f"   ✅ SL Order {sl_order.get('id')} is CORRECT")
                        else:
                            wrong_sl_orders.append(sl_order)

                    # ===== STEP 1: EDIT WRONG ORDERS ONLY =====
                    tp_edited = False
                    sl_edited = False
                    
                    if wrong_tp_orders and not tp_valid:
                        for tp_order in wrong_tp_orders:
                            order_id = tp_order.get("id")
                            print(f"   🔧 EDITING TP order {order_id}...")
                            edit_payload = {"id": order_id, "product_id": int(product_id), "order_type": "market_order", "stop_price": "{:.6f}".format(expected_tp), "size": abs(int(size))}
                            edit_body = json.dumps(edit_payload)
                            try:
                                edit_res = requests.put(BASE_URL + "/v2/orders", headers=sign_request("PUT", "/v2/orders", edit_body), data=edit_body, timeout=10)
                                if edit_res.status_code == 200:
                                    print(f"   ✅ TP EDITED successfully")
                                    tp_edited = True
                                    break
                            except: pass
                    
                    if wrong_sl_orders and not sl_valid:
                        for sl_order in wrong_sl_orders:
                            order_id = sl_order.get("id")
                            print(f"   🔧 EDITING SL order {order_id}...")
                            edit_payload = {"id": order_id, "product_id": int(product_id), "order_type": "market_order", "stop_price": "{:.6f}".format(expected_sl), "size": abs(int(size))}
                            edit_body = json.dumps(edit_payload)
                            try:
                                edit_res = requests.put(BASE_URL + "/v2/orders", headers=sign_request("PUT", "/v2/orders", edit_body), data=edit_body, timeout=10)
                                if edit_res.status_code == 200:
                                    print(f"   ✅ SL EDITED successfully")
                                    sl_edited = True
                                    break
                            except: pass

                    # ===== STEP 2: PLACE MISSING TP/SL (ONLY IF NOT EXISTS) =====
                    need_tp = not tp_valid and not tp_edited
                    need_sl = not sl_valid and not sl_edited
                    
                    if need_tp or need_sl:
                        # 🔥 SAFETY CHECK: Price check before adding new bracket
                        ticker = make_api_request('GET', f'/tickers/{symbol}')
                        if ticker:
                            curr_price = float(ticker['result']['close'])
                            # Check if price already hit or too close
                            is_safe = True
                            if size > 0: # Long
                                if expected_tp <= curr_price or expected_sl >= curr_price: is_safe = False
                            else: # Short
                                if expected_tp >= curr_price or expected_sl <= curr_price: is_safe = False
                            
                            if not is_safe:
                                print(f"   ⚠️ Price too close to TP/SL. Skipping placement to avoid error.")
                                continue

                        print(f"   📤 Placing missing TP/SL...")
                        payload = {
                            "product_id": int(product_id),
                            "take_profit_order": {"order_type": "market_order", "stop_price": "{:.6f}".format(expected_tp)},
                            "stop_loss_order": {"order_type": "market_order", "stop_price": "{:.6f}".format(expected_sl)}
                        }
                        body = json.dumps(payload)
                        try:
                            res = requests.post(BASE_URL + "/v2/orders/bracket", headers=sign_request("POST", "/v2/orders/bracket", body), data=body, timeout=10)
                            if res.status_code == 200:
                                print(f"   ✅ Bracket placed successfully")
                        except: pass
                    
                    time.sleep(0.3)

                except Exception as e:
                    print(f"   ❌ Error: {e}")

        except Exception as e:
            print(f"❌ Guardian error: {e}")
            time.sleep(2)




# ========== MAIN ==========
if __name__ == '__main__':
    init_database()
    
    # Start keepalive for Render
    print("Starting keepalive for Render...")
    start_keep_alive()
    print("Keepalive started - will ping every 10 minutes")
    
    # Start the signal bot
    print("Starting signal bot...")
    if start_signal_bot():
        print("Signal bot started successfully")
    else:
        print("Failed to start signal bot")
    
    # Start TP/SL guardian
    guardian_thread = threading.Thread(target=auto_tp_sl_guardian, daemon=True)
    guardian_thread.start()
    print("TP/SL Guardian started in background")
    
   
    try:
        app.run(debug=True, host='0.0.0.0', port=8090, use_reloader=False)
    finally:
        # Stop signal bot when app stops
        print("Stopping signal bot...")
        stop_signal_bot()
        print("Signal bot stopped")
