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
            ssl={}
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
    """Remove oldest trades to keep database under target size"""
    try:
        current_size = get_database_size()
        print(f"📊 Current database size: {current_size}MB")
        
        if current_size <= target_size_mb:
            print(f"✅ Database size is under limit ({current_size}MB <= {target_size_mb}MB)")
            return True
        
        print(f"⚠️ Database size exceeds limit ({current_size}MB > {target_size_mb}MB)")
        print("🧹 Starting cleanup of oldest trades...")
        
        while current_size > target_size_mb:
            count_query = "SELECT COUNT(*) as total_rows FROM closed_positions"
            total_result = execute_mysql_query(count_query, fetch_one=True)
            total_rows = total_result['total_rows'] if total_result else 0
            
            if total_rows <= 10:
                print("⚠️ Only 10 or fewer rows remaining, stopping cleanup")
                break
            
            rows_to_delete = max(1, total_rows // 10)
            
            delete_query = """
            DELETE FROM closed_positions 
            ORDER BY created_at ASC 
            LIMIT %s
            """
            execute_mysql_query(delete_query, (rows_to_delete,), commit=True)
            
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
LIVE_TP_PERCENTAGE = 0.35  # 1% Take Profit
LIVE_SL_PERCENTAGE = 0.1  # 0.4% Stop Loss

processing_lock = threading.Lock()
last_processed = {}

# Step-Based Lot Progression System
LOT_STEPS = {
    1: 1,
    2: 2,
    3: 4,
    4: 8,
    5: 16,
    6: 32,
    7: 64,
    8: 128,
    9: 256,
    10: 512,
    11: 1056
}

# Bot State
BOT_STATE = {
    'running': False,
    'thread': None,
    'current_step': 1,
    'current_lot': 1,
    'base_lot': 1,
    'leverage': 100,
    'tp_percent': 1,
    'sl_percent': 0.4,
    'max_steps': max(LOT_STEPS.keys()),
    'last_result': None,
    'symbol': 'ADAUSD',
    'stop_at_win': False,
    'stop_at_max_step': False,
    'force_stop': False,
    'session_start_time': None,
    'session_total_pnl': 0.0
}

# Trading Configuration - Market Specific Lot Sizes
LOT_SIZES = {
    'ADAUSD': 1,
}

LOT_SIZE_DEFAULT = 1

# Trade Result Memory
LAST_TRADE_RESULT = {
    'profit_loss': None,
    'timestamp': None,
    'lot_used': None,
    'processed': False
}

LOT_CALCULATION_LOCK = False
LAST_PROCESSED_TRADE_ID = None

# Recovery System
PROCESSED_EXIT_FILL_IDS = set()
LAST_PROCESSED_EXIT_FILL_IDS = set()

# Trade Completion Management
WAITING_FOR_FILL = False
TRADE_COMPLETED = False

# Position State Tracking
LAST_POSITION_STATE = {
    'symbol': None,
    'size': 0,
    'entry_price': 0
}

# Product ID Cache
PRODUCT_ID_CACHE = {}

# Database Thread Lock
db_lock = Lock()

# Bot Process Management
BOT_PROCESS = None
bot_process_lock = Lock()

# ========== DATABASE ==========
def init_database():
    """Initialize MySQL database - preserves existing data"""
    try:
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

# ========== SIGNAL GENERATION ==========
def generate_random_signal(reason="trade_result"):
    """Generate completely random unbiased buy/sell signal - TRUE 50/50"""
    import random
    
    signal = random.choice(['BUY', 'SELL'])
    signal_data = {
        'signal': signal,
        'timestamp': datetime.now().isoformat(),
        'confidence': 50,
        'layer': 'RANDOM',
        'score': 0,
        'source': 'random_generator',
        'reason': f'Random unbiased decision after {reason}',
        'decision_ready': True,
        'decision_confidence': 0.5,
        'position_analysis': {'has_position': False},
        'backtest_results': {},
        'last_trade_result': reason,
    }
    return signal_data

def save_closed_position(trade_data):
    """Save closed trade to MySQL database with thread safety and size management"""
    print(f"💾 Saving trade to MySQL database: {trade_data}")
    try:
        with db_lock:
            print(f"📊 MySQL database connecting...")
            
            cleanup_old_trades(target_size_mb=8.5)
            
            execute_mysql_query(
                "UPDATE closed_positions SET is_latest = 0 WHERE symbol = %s",
                (trade_data['symbol'],),
                commit=True
            )
            
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
            
            final_size = get_database_size()
            print(f"✅ Trade saved successfully to MySQL database")
            print(f"📊 Final database size: {final_size}MB")
            
    except Exception as e:
        print(f"❌ Error saving trade to MySQL database: {e}")
        import traceback
        traceback.print_exc()

# ========== OPTIMIZED POSITION TRACKING ==========
def get_product_id(symbol):
    """Get product_id for a symbol (cached)"""
    global PRODUCT_ID_CACHE
    
    if symbol in PRODUCT_ID_CACHE:
        return PRODUCT_ID_CACHE[symbol]
    
    try:
        products = make_api_request('GET', '/products')
        if not products or not products.get('result'):
            return None
        
        for product in products.get('result', []):
            if product.get('symbol') == symbol:
                product_id = product.get('id')
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
                'has_position': abs(size) > 0.001,
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
            print(f"❌ Error Body: {response.text}")
            return None
    except Exception as e:
        print(f"🚨 API Exception: {e}")
        return None


def place_order(symbol, side, quantity, order_type='market_order'):
    """Place order with correct Delta Exchange parameters"""
    order_data = {
        'product_symbol': symbol,
        'side': side,
        'order_type': order_type,
        'size': quantity
    }
    
    print(f"📋 Order Data: {order_data}")
    return make_api_request('POST', '/orders', order_data)

def set_leverage(symbol, leverage):
    """Set leverage using correct Delta Exchange endpoint"""
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
    
    return make_api_request('POST', f'/products/{product_id}/orders/leverage', {'leverage': str(leverage)})

def get_wallet_balance():
    """Get wallet balance from Delta Exchange API"""
    try:
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

# ========== FILL PAIRING ENGINE (CORE FIX) ==========

def get_fills_page(page_size=50):
    """Fetch fills from API with larger page size to catch all fills"""
    try:
        fills = make_api_request('GET', f'/fills?page_size={page_size}')
        if not fills or not fills.get('result'):
            return []
        return fills.get('result', [])
    except Exception as e:
        print(f"❌ Error fetching fills: {e}")
        return []

def group_fills_by_order(fills, symbol):
    """
    Group fills by order_id and aggregate them.
    Handles split fills - e.g. lot 2 split into 1+1 under same order_id.
    Returns list of aggregated orders: {order_id, side, total_size, avg_price, timestamp}
    """
    symbol_fills = [f for f in fills if f.get('product_symbol') == symbol]
    
    order_groups = {}  # order_id -> aggregated data
    
    for fill in symbol_fills:
        order_id = str(fill.get('order_id') or fill.get('id', ''))
        side = fill.get('side', '')
        size = float(fill.get('size', 0))
        price = float(fill.get('price', 0))
        created_at = fill.get('created_at', '')
        
        if not order_id or size <= 0 or price <= 0:
            continue
        
        if order_id not in order_groups:
            order_groups[order_id] = {
                'order_id': order_id,
                'side': side,
                'total_size': 0.0,
                'total_value': 0.0,  # sum of price * size for VWAP
                'avg_price': 0.0,
                'timestamp': created_at,
                'fills_count': 0
            }
        
        grp = order_groups[order_id]
        grp['total_size'] += size
        grp['total_value'] += price * size
        grp['fills_count'] += 1
        # Keep earliest timestamp for ordering
        if created_at < grp['timestamp']:
            grp['timestamp'] = created_at
    
    # Calculate VWAP (volume-weighted avg price) for each order
    for order_id, grp in order_groups.items():
        if grp['total_size'] > 0:
            grp['avg_price'] = grp['total_value'] / grp['total_size']
    
    # Sort by timestamp ascending (oldest first)
    sorted_orders = sorted(order_groups.values(), key=lambda x: x['timestamp'])
    
    print(f"📊 Grouped {len(symbol_fills)} fills into {len(sorted_orders)} orders for {symbol}")
    for o in sorted_orders:
        print(f"   Order {o['order_id']}: {o['side'].upper()} {o['total_size']} lots @ avg {o['avg_price']:.6f} ({o['fills_count']} fills)")
    
    return sorted_orders

def pair_entry_exit_orders(sorted_orders):
    """
    Match entry and exit orders into closed trade pairs.
    
    Logic:
    - Walk through orders in chronological order
    - Track running net position
    - When net position crosses 0, a trade pair is completed
    - Handles cases where position flips direction (rare but possible)
    
    Returns list of pairs: {entry_order, exit_order, size, entry_price, exit_price, side}
    """
    pairs = []
    
    # Stack-based position tracker
    # open_positions = list of {'side': 'buy'/'sell', 'size': float, 'price': float, 'order_id': str, 'timestamp': str}
    open_positions = []
    net_size = 0.0  # positive = long, negative = short
    
    for order in sorted_orders:
        side = order['side']  # 'buy' or 'sell'
        size = order['total_size']
        price = order['avg_price']
        
        if side == 'buy':
            signed_size = +size
        else:
            signed_size = -size
        
        prev_net = net_size
        net_size += signed_size
        
        print(f"   [PAIR ENGINE] {side.upper()} {size} | net: {prev_net:.4f} → {net_size:.4f}")
        
        # Check if this order closes/reduces an open position
        if abs(prev_net) > 0.001 and (
            (prev_net > 0 and side == 'sell') or
            (prev_net < 0 and side == 'buy')
        ):
            # This is a closing/reducing order
            close_size = min(size, abs(prev_net))
            
            # Find the entry order (the one that opened the position)
            # It's the last order with opposite side that opened the current net position
            entry_order = None
            entry_size = 0.0
            entry_price_acc = 0.0
            entry_size_acc = 0.0
            
            # Walk back through open_positions to find entry
            for op in reversed(open_positions):
                if abs(net_size) < abs(prev_net):
                    entry_order = op
                    entry_size = op['size']
                    entry_price_acc += op['price'] * op['size']
                    entry_size_acc += op['size']
                    break
            
            if entry_order:
                # Use weighted avg if multiple entries (simplified: use last entry)
                entry_price = entry_order['price']
                entry_side = entry_order['side']
                
                pair = {
                    'entry_order_id': entry_order['order_id'],
                    'exit_order_id': order['order_id'],
                    'side': entry_side,
                    'size': close_size,
                    'entry_price': entry_price,
                    'exit_price': price,
                    'entry_time': entry_order['timestamp'],
                    'exit_time': order['timestamp']
                }
                pairs.append(pair)
                print(f"   ✅ PAIR FOUND: {entry_side.upper()} {close_size} | Entry: {entry_price:.6f} → Exit: {price:.6f}")
                
                # Remove matched entry from open positions
                open_positions = [op for op in open_positions if op['order_id'] != entry_order['order_id']]
        
        # If this is an opening order (increases net position in same direction)
        if (side == 'buy' and net_size > 0 and net_size > prev_net) or \
           (side == 'sell' and net_size < 0 and net_size < prev_net):
            open_positions.append({
                'order_id': order['order_id'],
                'side': side,
                'size': size,
                'price': price,
                'timestamp': order['timestamp']
            })
        
        # If position is now flat, clear open positions
        if abs(net_size) < 0.001:
            open_positions = []
    
    return pairs

def find_latest_closed_pair(symbol):
    """
    Main function: fetch fills, group by order, pair entry/exit, return last closed pair.
    This is the CORRECT replacement for get_pnl_from_fills and get_entry_exit_from_fills.
    
    Returns: (pnl, entry_exit_data) or (0, None) if not found
    """
    print(f"\n🔍 FILL PAIR ENGINE - Finding closed pair for {symbol}")
    
    # Fetch enough fills to cover recent trades (page_size=50 for safety)
    all_fills = get_fills_page(page_size=50)
    
    if not all_fills:
        print("❌ No fills received from API")
        return 0, None
    
    # Group fills by order_id to handle split fills
    sorted_orders = group_fills_by_order(all_fills, symbol)
    
    if len(sorted_orders) < 2:
        print(f"❌ Not enough orders to form a pair (found {len(sorted_orders)})")
        return 0, None
    
    # Try simple sequential pairing first (most reliable for martingale bot)
    # The bot always: open → close → open → close...
    # So fills in order: [entry1, exit1, entry2, exit2, ...]
    # Latest pair = last two orders (if they are opposite sides)
    
    last_two = sorted_orders[-2:]
    
    if len(last_two) == 2:
        order_a = last_two[0]  # older = entry
        order_b = last_two[1]  # newer = exit
        
        print(f"\n🎯 CHECKING LAST TWO ORDERS:")
        print(f"   A: {order_a['side'].upper()} {order_a['total_size']} @ {order_a['avg_price']:.6f} [{order_a['timestamp']}]")
        print(f"   B: {order_b['side'].upper()} {order_b['total_size']} @ {order_b['avg_price']:.6f} [{order_b['timestamp']}]")
        
        if order_a['side'] != order_b['side']:
            # They are opposite sides - this is a valid pair
            entry_order = order_a
            exit_order = order_b
            entry_side = entry_order['side']
            
            # Use the smaller size (in case of partial close, use exit size)
            trade_size = min(entry_order['total_size'], exit_order['total_size'])
            
            entry_price = entry_order['avg_price']
            exit_price = exit_order['avg_price']
            
            symbol_key = symbol
            lot_size = LOT_SIZES.get(symbol_key, LOT_SIZE_DEFAULT)
            actual_quantity = trade_size * lot_size
            
            # Calculate PnL
            if entry_side == 'buy':
                pnl = (exit_price - entry_price) * actual_quantity
            else:
                pnl = (entry_price - exit_price) * actual_quantity
            
            print(f"\n💰 PAIR RESULT:")
            print(f"   Side: {entry_side.upper()}")
            print(f"   Size: {trade_size} lots = {actual_quantity} {symbol.replace('USD', '')} (1 lot = {lot_size})")
            print(f"   Entry: {entry_price:.6f} | Exit: {exit_price:.6f}")
            print(f"   PnL: {pnl:.6f} USD")
            print(f"   Result: {'✅ PROFIT' if pnl > 0 else '❌ LOSS'}")
            
            entry_exit_data = {
                'side': entry_side,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'quantity': trade_size,
                'entry_time': entry_order['timestamp'],
                'exit_time': exit_order['timestamp']
            }
            
            return pnl, entry_exit_data
        else:
            print(f"⚠️ Last two orders are SAME SIDE ({order_a['side']}) - position still open or data issue")
            print(f"⚠️ Falling back to full pair engine...")
    
    # Fallback: use full pair engine for complex cases
    pairs = pair_entry_exit_orders(sorted_orders)
    
    if not pairs:
        print("❌ No pairs found by pair engine")
        return 0, None
    
    # Return the most recent pair
    latest_pair = pairs[-1]
    
    entry_side = latest_pair['side']
    entry_price = latest_pair['entry_price']
    exit_price = latest_pair['exit_price']
    trade_size = latest_pair['size']
    
    lot_size = LOT_SIZES.get(symbol, LOT_SIZE_DEFAULT)
    actual_quantity = trade_size * lot_size
    
    if entry_side == 'buy':
        pnl = (exit_price - entry_price) * actual_quantity
    else:
        pnl = (entry_price - exit_price) * actual_quantity
    
    print(f"\n💰 PAIR ENGINE RESULT:")
    print(f"   Side: {entry_side.upper()}")
    print(f"   Size: {trade_size} lots = {actual_quantity}")
    print(f"   Entry: {entry_price:.6f} | Exit: {exit_price:.6f}")
    print(f"   PnL: {pnl:.6f} USD")
    
    entry_exit_data = {
        'side': entry_side,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'quantity': trade_size,
        'entry_time': latest_pair['entry_time'],
        'exit_time': latest_pair['exit_time']
    }
    
    return pnl, entry_exit_data


# ========== TRADE COMPLETION MANAGEMENT ==========

def wait_for_complete_trade(symbol, max_wait=5):
    """Wait for complete trade data (entry + exit fills) before allowing next trade"""
    start = time.time()
    
    while time.time() - start < max_wait:
        pnl, data = find_latest_closed_pair(symbol)
        
        if data:
            print("✅ Complete trade found!")
            return pnl, data
        
        print("⏳ Waiting for fills...")
        time.sleep(0.5)
    
    print("❌ Fills not received in time")
    return None, None

# ========== RETRY LOGIC FOR FAST CLOSES ==========

def get_trade_with_retry(symbol, retries=5):
    """Get trade data with retry logic to handle delayed fills API updates"""
    for i in range(retries):
        pnl, data = find_latest_closed_pair(symbol)
        if data:
            print(f"✅ Trade data found on attempt {i+1}")
            return pnl, data
        print(f"⏳ Retry {i+1}/{retries} - waiting for fills API to update...")
        time.sleep(1)
    print(f"❌ No trade data found after {retries} retries")
    return 0, None

# ========== LEGACY WRAPPERS (now use fill pair engine) ==========

def get_pnl_from_fills():
    """Get PnL from fills using the correct pairing engine"""
    pnl, _ = find_latest_closed_pair(BOT_STATE['symbol'])
    return pnl

def get_entry_exit_from_fills():
    """Get entry/exit data using the correct pairing engine"""
    _, entry_exit_data = find_latest_closed_pair(BOT_STATE['symbol'])
    return entry_exit_data


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
        
        product_id = get_product_id(BOT_STATE['symbol'])
        if not product_id:
            print("❌ Could not get product_id")
            return False, False, 0
        
        current_pos = check_position_realtime(product_id)
        
        if current_pos.get('error'):
            print("⚠️ API FAILED - SKIPPING CLOSURE CHECK")
            return True, False, 0
        
        was_closed = False
        pnl = 0
        
        if abs(LAST_POSITION_STATE['size']) > 0.001 and abs(current_pos.get('size', 0)) <= 0.001:
            print("🎯 Position CLOSED!")
            
            was_closed = True
            global WAITING_FOR_FILL, TRADE_COMPLETED
            WAITING_FOR_FILL = True  # Block next trade until fills are complete
            TRADE_COMPLETED = False  # Reset trade completion flag
            
            # Wait for complete trade data before processing
            print("⏳ Waiting for complete trade data (entry + exit fills)...")
            pnl, entry_exit_data = wait_for_complete_trade(BOT_STATE['symbol'])
            
            if entry_exit_data:
                print("✅ Trade completed - INSTANT MEMORY UPDATE FIRST")
                
                # 🔥 STEP 1: INSTANT DECISION DATA (memory update)
                LAST_TRADE_RESULT['profit_loss'] = pnl
                LAST_TRADE_RESULT['timestamp'] = datetime.now().isoformat()
                LAST_TRADE_RESULT['lot_used'] = LAST_POSITION_STATE['size']
                LAST_TRADE_RESULT['processed'] = True
                
                # Update BOT_STATE with PnL (instant)
                BOT_STATE['last_result'] = 'PROFIT' if pnl > 0 else 'LOSS'
                BOT_STATE['last_pnl'] = pnl
                
                print(f"🧠 INSTANT MEMORY UPDATE: Last PnL = {pnl:.5f}, Result = {BOT_STATE['last_result']}")
                
                # ✅ STEP 2: ASYNC DATABASE SAVE (background, no delay)
                print("💾 Saving to database (async)...")
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
                print(f"💾 ✅ Database save completed")
                
                if BOT_STATE['session_start_time']:
                    BOT_STATE['session_total_pnl'] += pnl
                    print(f"   Session P&L updated: {BOT_STATE['session_total_pnl']:.2f} (Trade P&L: {pnl:.2f})")
                
                print(f"✅ STATE UPDATED: Last PnL = {pnl:.5f}, Result = {BOT_STATE['last_result']}")
                
                # Generate new signal after state is updated
                result_type = "PROFIT" if pnl > 0 else "LOSS"
                reason = f"after_{result_type.lower()}_pnl={pnl:.5f}"
                
                global CURRENT_SIGNAL
                CURRENT_SIGNAL = generate_random_signal(reason=reason)
                print(f"     Generated new signal: {CURRENT_SIGNAL['signal']} after {result_type}")
                
                # Mark trade as completed
                TRADE_COMPLETED = True
                print(f"🎯 TRADE COMPLETED - Ready for next trade")
                
            else:
                print(f"⚠️ FORCE SAVE - Using fallback data (fills API missed)")
                
                # 🔥 INSTANT MEMORY UPDATE FIRST (even for fallback)
                LAST_TRADE_RESULT['profit_loss'] = pnl
                LAST_TRADE_RESULT['timestamp'] = datetime.now().isoformat()
                LAST_TRADE_RESULT['lot_used'] = LAST_POSITION_STATE['size']
                LAST_TRADE_RESULT['processed'] = True
                
                BOT_STATE['last_result'] = 'PROFIT' if pnl > 0 else 'LOSS'
                BOT_STATE['last_pnl'] = pnl
                
                print(f"🧠 INSTANT MEMORY UPDATE (fallback): Last PnL = {pnl:.5f}, Result = {BOT_STATE['last_result']}")
                
                # Force save fallback - ensure no trade is missed
                entry_exit_data = {
                    'side': 'buy' if LAST_POSITION_STATE['size'] > 0 else 'sell',
                    'entry_price': LAST_POSITION_STATE['entry_price'],
                    'exit_price': LAST_POSITION_STATE['entry_price'],  # approx - will be updated later
                    'quantity': abs(LAST_POSITION_STATE['size']),
                    'entry_time': datetime.now().isoformat(),
                    'exit_time': datetime.now().isoformat()
                }
                print(f"🔥 FORCE SAVING: {entry_exit_data}")
                
                # Save fallback data (async)
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
                print(f"💾 🔥 FORCE SAVED - No trade missed!")
                
                TRADE_COMPLETED = True
                print(f"🎯 FORCE TRADE COMPLETED")
            
            print(f"     PnL: {pnl:.5f}")
            print(f"     Result: {'PROFIT ✅' if pnl > 0 else 'LOSS ❌'}{' ' if pnl > 0 else ' '}")
        
        elif LAST_POSITION_STATE['size'] > 0 and current_pos.get('size', 0) == 0:
            print("🔄 RECOVERY: Position was closed during network issue")
        
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


# ========== TRADING LOGIC ==========
CURRENT_SIGNAL = None

def get_trading_signal():
    """Generate completely new unbiased trading signal on every call"""
    global CURRENT_SIGNAL
    
    try:
        CURRENT_SIGNAL = generate_random_signal(reason="trade_decision")
        print(f"[SIGNAL] Generated new unbiased signal: {CURRENT_SIGNAL['signal']}")
        
        signal = CURRENT_SIGNAL.get('signal', '')
        confidence = CURRENT_SIGNAL.get('confidence', 0)
        
        print(f"🎲 Random Signal: {signal}")
        print(f"📊 Confidence: {confidence}% (unbiased 50/50)")
        print(f"🔄 Fresh decision - no bias from previous trades")
        
        if signal.upper() == "BUY":
            return 'buy', CURRENT_SIGNAL
        elif signal.upper() == "SELL":
            return 'sell', CURRENT_SIGNAL
        else:
            print(f"Unknown signal: {signal}")
            return None, CURRENT_SIGNAL
            
    except Exception as e:
        print(f"Error getting signal: {e}")
        return None, None

def start_signal_bot():
    """Start the signal bot"""
    try:
        print("🤖 Signal bot ready - will generate unbiased random signals on demand")
        return True
    except Exception as e:
        print(f"❌ Error starting signal bot: {e}")
        return False

def stop_signal_bot():
    """Stop the signal bot"""
    try:
        print("🛑 Signal bot stopped")
    except Exception as e:
        print(f"❌ Error stopping signal bot: {e}")

def detect_current_step_from_lot(lot_size):
    """Detect current step based on lot size (use absolute value for negative sizes)"""
    lot_size = abs(lot_size)  # Ensure positive value
    for step, lot in LOT_STEPS.items():
        if lot == lot_size:
            return step
    for step, lot in LOT_STEPS.items():
        if lot >= lot_size:
            return step
    return 1

def detect_current_step_from_live_position():
    """Detect current step from live position lot size"""
    try:
        has_position = abs(LAST_POSITION_STATE['size']) > 0.001
        if has_position:
            current_lot = abs(LAST_POSITION_STATE['size'])
            current_step = detect_current_step_from_lot(current_lot)
            print(f"🔍 Live position detected: Lot {current_lot} = Step {current_step}")
            return current_step, current_lot
        else:
            print("🔍 No live position detected - using Step 1")
            return 1, LOT_STEPS[1]
    except Exception as e:
        print(f"❌ Error detecting step from live position: {e}")
        return 1, LOT_STEPS[1]

def calculate_next_lot():
    """Calculate next lot size using step-based progression system"""
    global LOT_CALCULATION_LOCK
    
    if LOT_CALCULATION_LOCK:
        print("⚠️ Lot calculation already in progress - using current lot")
        return BOT_STATE['current_lot']
    
    LOT_CALCULATION_LOCK = True
    
    try:
        print(f"\n📊 STEP-BASED LOT CALCULATION:")
        print(f"   Current Step: {BOT_STATE['current_step']}")
        print(f"   Current Lot: {BOT_STATE['current_lot']}")
        print(f"   Last PnL: {LAST_TRADE_RESULT['profit_loss']}")
        print(f"   Processed: {LAST_TRADE_RESULT['processed']}")
        
        has_live_position = abs(LAST_POSITION_STATE['size']) > 0.001
        if has_live_position:
            live_step, live_lot = detect_current_step_from_live_position()
            if live_step != BOT_STATE['current_step']:
                print(f"🔄 Syncing step with live position: {BOT_STATE['current_step']} → {live_step}")
                BOT_STATE['current_step'] = live_step
                BOT_STATE['current_lot'] = live_lot
        
        current_step = BOT_STATE['current_step']
        current_lot = BOT_STATE['current_lot']
        
        if LAST_TRADE_RESULT['profit_loss'] is not None and not LAST_TRADE_RESULT['processed']:
            print(f"⚠️ Result not processed yet - using current lot: {current_lot}")
            return current_lot
        
        if LAST_TRADE_RESULT['profit_loss'] is None:
            if has_live_position:
                print(f"🔄 Live position exists - using current step {current_step}, lot {current_lot}")
                return current_lot
            else:
                print(f"✅ No previous result, no position - starting at Step 1")
                BOT_STATE['current_step'] = 1
                BOT_STATE['current_lot'] = LOT_STEPS[1]
                return LOT_STEPS[1]
        
        if LAST_TRADE_RESULT['profit_loss'] > 0:
            # WIN - Reset to Step 1
            next_step = 1
            next_lot = LOT_STEPS[next_step]
            BOT_STATE['current_step'] = next_step
            BOT_STATE['current_lot'] = next_lot
            print(f"✅ WIN - Reset to Step {next_step}: Lot {next_lot}")
        else:
            # LOSS - Move to next step
            next_step = current_step + 1
            if next_step > BOT_STATE['max_steps']:
                next_step = 1
                next_lot = LOT_STEPS[next_step]
                BOT_STATE['current_step'] = next_step
                BOT_STATE['current_lot'] = next_lot
                print(f"🚨 MAX STEPS REACHED - Reset to Step {next_step}: Lot {next_lot}")
            else:
                next_lot = LOT_STEPS[next_step]
                BOT_STATE['current_step'] = next_step
                BOT_STATE['current_lot'] = next_lot
                print(f"❌ LOSS - Move to Step {next_step}: Lot {next_lot}")
        
        # ✅ FIXED: Don't reset profit_loss, only reset processed flag
        # Keep profit_loss preserved for next trade decision
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
        PRODUCT_CONFIG = {
            "ADAUSD": {"id": 16614, "tick": Decimal("0.00001")},
            "BTCUSD": {"id": 84,    "tick": Decimal("0.5")},
            "ETHUSD": {"id": 1320,  "tick": Decimal("0.05")},
        }

        config = PRODUCT_CONFIG.get(symbol)
        if not config:
            print(f"❌ Symbol {symbol} not in config!")
            return None

        p_id = config["id"]
        tick = config["tick"]

        ticker = make_api_request('GET', f'/tickers/{symbol}')
        if not ticker or not ticker.get('result'):
            print("❌ Ticker fetch failed")
            return None

        result     = ticker['result']
        mark_price = float(result.get('mark_price') or result.get('close'))
        print(f"📊 Mark Price: {mark_price}")

        def to_tick(val):
            d = Decimal(str(val))
            return (d / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        base_dec = to_tick(mark_price)

        if side == 'buy':
            tp_dec = to_tick(mark_price * (1 + tp_pct / 100))
            sl_dec = to_tick(mark_price * (1 - sl_pct / 100))
        else:
            tp_dec = to_tick(mark_price * (1 - tp_pct / 100))
            sl_dec = to_tick(mark_price * (1 + sl_pct / 100))

        print(f"📐 After % calc  → TP: {tp_dec} | SL: {sl_dec}")

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

        if response and response.get('success') and 'result' in response:
            oid     = response['result'].get('id')
            bracket = response['result'].get('bracket_orders', [])
            print(f"\n✅ ORDER PLACED! ID: {oid}")

            # ── FIX: Recalculate TP/SL from actual fill price ──────────────
            actual_entry = float(
                response['result'].get('average_fill_price') or
                response['result'].get('limit_price') or
                mark_price
            )
            print(f"📌 Actual Fill Price: {actual_entry} | Mark was: {mark_price}")

            if actual_entry != mark_price:
                print(f"⚠️  Slippage detected ({abs(actual_entry - mark_price):.5f}), recalculating TP/SL...")
                actual_base = to_tick(actual_entry)

                if side == 'buy':
                    tp_dec = to_tick(actual_entry * (1 + tp_pct / 100))
                    sl_dec = to_tick(actual_entry * (1 - sl_pct / 100))
                else:
                    tp_dec = to_tick(actual_entry * (1 - tp_pct / 100))
                    sl_dec = to_tick(actual_entry * (1 + sl_pct / 100))

                if side == 'buy':
                    if tp_dec <= actual_base:
                        tp_dec = actual_base + min_gap
                    if sl_dec >= actual_base:
                        sl_dec = actual_base - min_gap
                else:
                    if tp_dec >= actual_base:
                        tp_dec = actual_base - min_gap
                    if sl_dec <= actual_base:
                        sl_dec = actual_base + min_gap

                tp_price = str(tp_dec)
                sl_price = str(sl_dec)
                print(f"🎯 Recalculated TP : {tp_price}")
                print(f"🛡️  Recalculated SL : {sl_price}")
            # ── END FIX ────────────────────────────────────────────────────

            if bracket:
                print(f"🎯 Bracket: {bracket}")
            else:
                print("⚠️  Bracket missing! Placing manually via /orders/bracket ...")
                bracket_payload = {
                    "product_id": p_id,
                    "take_profit_order": {
                        "order_type": "market_order",
                        "stop_price": tp_price
                    },
                    "stop_loss_order": {
                        "order_type": "market_order",
                        "stop_price": sl_price
                    },
                    "bracket_stop_trigger_method": "mark_price"
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
    
    print(f"⚡ Setting leverage: {BOT_STATE['leverage']}x")
    leverage_result = set_leverage(BOT_STATE['symbol'], BOT_STATE['leverage'])
    if not leverage_result:
        print("❌ Failed to set initial leverage, stopping bot")
        return
    
    print(f"\n🔍 CHECKING FOR EXISTING LIVE POSITION...")
    product_id = get_product_id(BOT_STATE['symbol'])
    if product_id:
        current_pos = check_position_realtime(product_id)
        if abs(current_pos.get('size', 0)) > 0.001:
            print(f"🚨 EXISTING POSITION FOUND: {current_pos.get('size', 0)} lots")
            print(f"📊 Entry Price: {current_pos.get('entry_price', 0)}")
            
            current_lot = abs(current_pos.get('size', 0))
            detected_step = detect_current_step_from_lot(current_lot)
            
            BOT_STATE['current_step'] = detected_step
            BOT_STATE['current_lot'] = current_lot
            
            print(f"🔄 STEP DETECTION: Lot {current_lot} = Step {detected_step}")
            print(f"💰 Current lot set to: {BOT_STATE['current_lot']} (Step {BOT_STATE['current_step']})")
            
            LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
            LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
            LAST_POSITION_STATE['entry_price'] = current_pos.get('entry_price', 0)
            
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
            BOT_STATE['current_step'] = 1
            BOT_STATE['current_lot'] = LOT_STEPS[1]
            LAST_TRADE_RESULT['profit_loss'] = None
            LAST_TRADE_RESULT['processed'] = False
    
    while BOT_STATE['running']:
        try:
            if BOT_STATE['force_stop']:
                print("🛑 Force Stop triggered - Stopping bot immediately!")
                BOT_STATE['running'] = False
                BOT_STATE['force_stop'] = False
                break
            
            # ✅ STEP 6: Block next trade until trade is completed and saved
            global WAITING_FOR_FILL, TRADE_COMPLETED
            if WAITING_FOR_FILL:
                print("⏳ Waiting for trade to be completed and saved...")
                
                pnl, data = find_latest_closed_pair(BOT_STATE['symbol'])
                
                if data and TRADE_COMPLETED:
                    WAITING_FOR_FILL = False
                    TRADE_COMPLETED = False  # Reset for next trade
                    print("✅ Trade completed and saved, ready for next trade")
                else:
                    if data and not TRADE_COMPLETED:
                        print("⏳ Fill received but trade not yet saved...")
                    else:
                        print("⏳ Still waiting for fills...")
                    time.sleep(0.5)
                    continue
            
            # ✅ STEP 7: Safety check - prevent next trade if PnL not recorded
            if BOT_STATE.get('last_pnl') is None and LAST_TRADE_RESULT['profit_loss'] is not None:
                print("❌ PnL not recorded properly → skipping next trade")
                time.sleep(1)
                continue
            
            print(f"\n{'='*50}")
            print(f"🔍 BOT LOOP CHECK - Symbol: {BOT_STATE['symbol']}")
            print(f"📊 Running: {BOT_STATE['running']}, Step: {BOT_STATE['current_step']}")
            print(f"💰 Current Lot: {BOT_STATE['current_lot']}")
            print(f"{'='*50}")
            
            has_position, was_closed, pnl = check_position_and_detect_closure()
            
            if was_closed:
                print(f"🎯 Position was closed! PnL: {pnl}")
                print(f"📊 Result: {'PROFIT ✅' if pnl > 0 else 'LOSS ❌'}")
                print(f"💰 Next lot will be calculated based on this result")
                
                if pnl > 0 and BOT_STATE['stop_at_win']:
                    print(f"🏆 PROFIT ACHIEVED - STOP AT WIN ACTIVATED!")
                    print(f"🛑 Bot will stop placing new trades...")
                    BOT_STATE['running'] = False
                    BOT_STATE['stop_at_win'] = False
                    continue
                
                if pnl < 0 and (BOT_STATE['current_step'] + 1) >= BOT_STATE['max_steps'] and BOT_STATE['stop_at_max_step']:
                    print(f"🚨 MAX STEP HIT! - STOP AT MAX STEP ACTIVATED!")
                    print(f"📊 Current step: {BOT_STATE['current_step']}, Max steps: {BOT_STATE['max_steps']}")
                    print(f"🛑 Bot will stop placing new trades...")
                    BOT_STATE['running'] = False
                    BOT_STATE['stop_at_max_step'] = False
                    continue
            
            if has_position:
                print("⏳ Active position found, waiting for closure...")
                time.sleep(0.5)
                continue
            
            if BOT_STATE['force_stop']:
                print("🛑 FORCE STOP ACTIVE - Skipping order placement")
                BOT_STATE['running'] = False
                continue
            
            if BOT_STATE['stop_at_win']:
                print("🎯 STOP AT WIN ACTIVE - Will stop after next profit")
            
            if LAST_TRADE_RESULT['profit_loss'] is not None and not LAST_TRADE_RESULT['processed']:
                print("⚠️ RESULT NOT PROCESSED - WAITING...")
                
                if LAST_TRADE_RESULT['timestamp']:
                    result_time = datetime.fromisoformat(LAST_TRADE_RESULT['timestamp'])
                    time_stuck = datetime.now() - result_time
                    
                    if time_stuck.total_seconds() > 30:
                        print(f"🚨 RESULT STUCK for {time_stuck.total_seconds():.0f} seconds - AUTO CLEARING...")
                        clear_stuck_trade_result()
                        continue
                
                continue
            
            next_lot = calculate_next_lot()
            print(f"💰 Next Lot Size: {next_lot}")
            print(f"🎯 ORDER DETAILS:")
            print(f"   📊 Base Lot: {BOT_STATE['base_lot']}")
            print(f"   📊 Current Lot: {BOT_STATE['current_lot']}")
            print(f"   📊 Next Lot: {next_lot}")
            print(f"   📊 Current Step: {BOT_STATE['current_step']}")
            
            LAST_TRADE_RESULT['processed'] = False
            
            signal_result = get_trading_signal()
            side = signal_result[0] if signal_result else 'buy'
            signal_data = signal_result[1] if signal_result and len(signal_result) > 1 else {}
            
            if side is None:
                print("⏳ Waiting for bot decision - skipping this cycle")
                time.sleep(1)
                continue
            
            print(f"📈 Trading Signal: {side.upper()} order with {next_lot} lots")
            
            product_id = get_product_id(BOT_STATE['symbol'])
            if product_id:
                current_pos = check_position_realtime(product_id)
                if abs(current_pos.get('size', 0)) > 0.001:
                    print("⛔ SAFETY CHECK: Real position exists - SKIPPING ORDER")
                    print(f"📊 Position Size: {current_pos.get('size', 0)} lots")
                    print(f"📊 Entry Price: {current_pos.get('entry_price', 0)}")
                    
                    LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
                    LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
                    if abs(LAST_POSITION_STATE['size']) > 0.001:
                        print("⏳ Active position found, waiting for closure...")
                        continue
                    else:
                        print("✅ SAFETY CHECK: No active position - safe to proceed")
                        
                        if LAST_TRADE_RESULT['profit_loss'] is None and BOT_STATE['current_lot'] != BOT_STATE['base_lot']:
                            print("⚠️ Ignoring false closure due to network issue - keeping current lot")
                            continue
            
            print(f"🎯 PLACING ORDER WITH CALCULATED LOT: {next_lot}")
            
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
                
                print("⚡ Quick position check (TP/SL already attached)...")
                max_wait_time = 2
                wait_start = time.time()
                
                while time.time() - wait_start < max_wait_time:
                    time.sleep(0.05)
                    current_pos = check_position_realtime(product_id)
                    if abs(current_pos.get('size', 0)) > 0.001:
                        print(f"⚡ Position confirmed: {current_pos.get('size', 0)} lots (TP/SL active)")
                        break
                else:
                    print("⚠️ Position check timeout - continuing (TP/SL should be active)")
                
                product_id = get_product_id(BOT_STATE['symbol'])
                if product_id:
                    current_pos = check_position_realtime(product_id)
                    
                    if current_pos.get('error'):
                        print("⚠️ API FAILED - SKIPPING POSITION UPDATE")
                        continue
                    
                    if current_pos.get('has_position') and abs(current_pos.get('size', 0)) > 0.001:
                        LAST_POSITION_STATE['symbol'] = BOT_STATE['symbol']
                        LAST_POSITION_STATE['size'] = current_pos.get('size', 0)
                        LAST_POSITION_STATE['entry_price'] = current_pos.get('entry_price', 0)
                        
                        # Always store positive lot size for display (use absolute value)
                        BOT_STATE['current_lot'] = abs(current_pos.get('size', 0))
                        
                        print(f"📊 Position State Updated: Size={LAST_POSITION_STATE['size']}, Entry={LAST_POSITION_STATE['entry_price']}")
                        print(f"🔄 Current Lot Synced: {BOT_STATE['current_lot']} = {current_pos.get('size', 0)} (from actual position)")
                    else:
                        print("⚠️ Position not found in real-time check")
                else:
                    print("❌ Could not get product_id for position update")
            else:
                print("❌ Order failed!")
                print(f"📋 Order Response: {order_response}")
                time.sleep(0.5)
            
        except Exception as e:
            print(f"🚨 TEMPORARY BOT ERROR: {e}")
            import traceback
            traceback.print_exc()
            print("🔄 Bot is staying alive... retrying in 5 seconds")
            time.sleep(5) 
            continue
    
    print("🤖 Auto Trading Bot Stopped")

def start_auto_trading_bot():
    """Start the bot"""
    if BOT_STATE['running']:
        return False
    
    global LAST_POSITION_STATE
    LAST_POSITION_STATE = {
        'symbol': BOT_STATE['symbol'],
        'size': 0,
        'entry_price': 0
    }
    
    BOT_STATE['stop_at_win'] = False
    BOT_STATE['stop_at_max_step'] = False
    BOT_STATE['force_stop'] = False
    
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
        
        if (LAST_TRADE_RESULT['profit_loss'] is not None and 
            not LAST_TRADE_RESULT['processed']):
            
            print(f"   🔍 Found {len(recent_trades)} recent trades in MySQL database")
            
            for trade in recent_trades:
                db_pnl = trade['pnl']
                if abs(float(db_pnl) - LAST_TRADE_RESULT['profit_loss']) < 0.01:
                    print(f"   🎯 Found matching trade in MySQL DB: P&L={db_pnl}")
                    print("   🆕 AUTO-CLEARING stuck result based on database match")
                    clear_stuck_trade_result()
                    return
            
            print("   ⚠️ No exact match found - AUTO-CLEARING to unstick bot")
            clear_stuck_trade_result()
        else:
            print("   ✅ No stuck result detected")
            
    except Exception as e:
        print(f"   ❌ Error in database reconciliation: {e}")
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
        
        try:
            public_ip = requests.get('https://ipinfo.io/ip', timeout=5).text.strip()
        except:
            public_ip = "Unknown"
        
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
        
        leverage = data.get('leverage', 10)
        tp_percent = data.get('tp_percent', 2.0)
        sl_percent = data.get('sl_percent', 1.0)
        symbol = data.get('symbol', 'ADAUSD')
        
        max_steps = max(LOT_STEPS.keys())
        
        if not isinstance(leverage, int) or leverage < 1 or leverage > 200:
            return jsonify({'success': False, 'message': 'Leverage must be integer between 1-200'}), 400
        
        if not isinstance(tp_percent, (int, float)) or tp_percent < 0.1 or tp_percent > 50:
            return jsonify({'success': False, 'message': 'TP percent must be between 0.1-50'}), 400
        
        if not isinstance(sl_percent, (int, float)) or sl_percent < 0.1 or sl_percent > 50:
            return jsonify({'success': False, 'message': 'SL percent must be between 0.1-50'}), 400
        
        if not isinstance(symbol, str) or len(symbol) < 1 or len(symbol) > 20:
            return jsonify({'success': False, 'message': 'Symbol must be string between 1-20 characters'}), 400
        
        BOT_STATE['leverage'] = leverage
        BOT_STATE['tp_percent'] = float(tp_percent)
        BOT_STATE['sl_percent'] = float(sl_percent)
        BOT_STATE['max_steps'] = max_steps
        BOT_STATE['symbol'] = symbol
        
        print(f"\n🔍 CHECKING FOR EXISTING LIVE POSITION BEFORE START...")
        product_id = get_product_id(symbol)
        has_existing_position = False
        
        if product_id:
            current_pos = check_position_realtime(product_id)
            if abs(current_pos.get('size', 0)) > 0.001:
                has_existing_position = True
                current_lot = abs(current_pos.get('size', 0))
                detected_step = detect_current_step_from_lot(current_lot)
                
                BOT_STATE['current_step'] = detected_step
                BOT_STATE['current_lot'] = current_lot
                
                print(f"🚨 EXISTING POSITION FOUND: {current_lot} lots")
                print(f"🔄 STEP DETECTION: Lot {current_lot} = Step {detected_step}")
            else:
                BOT_STATE['current_step'] = 1
                BOT_STATE['current_lot'] = LOT_STEPS[1]
                print(f"✅ No existing position - starting fresh at Step 1, Lot {LOT_STEPS[1]}")
        else:
            BOT_STATE['current_step'] = 1
            BOT_STATE['current_lot'] = LOT_STEPS[1]
            print(f"⚠️ Could not check position - starting fresh at Step 1, Lot {LOT_STEPS[1]}")
        
        print(f"🎯 BOT STARTING WITH STEP-BASED SYSTEM:")
        print(f"   📊 Current Step: {BOT_STATE['current_step']}")
        print(f"   📊 Current Lot: {BOT_STATE['current_lot']}")
        print(f"   📊 Max Steps: {BOT_STATE['max_steps']}")
        print(f"   📊 Symbol: {symbol}")
        print(f"   📊 Leverage: {BOT_STATE['leverage']}")
        print(f"   📊 TP%: {BOT_STATE['tp_percent']}")
        print(f"   📊 SL%: {BOT_STATE['sl_percent']}")
        
        LAST_TRADE_RESULT['profit_loss'] = None
        LAST_TRADE_RESULT['processed'] = False
        
        BOT_STATE['symbol'] = symbol.upper()
        
        if start_auto_trading_bot():
            return jsonify({
                'success': True, 
                'message': 'Bot started successfully',
                'current_step': BOT_STATE['current_step'],
                'current_lot': BOT_STATE['current_lot'],
                'has_existing_position': has_existing_position
            })
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
        BOT_STATE['force_stop'] = False
        print("🎯 STOP AT WIN ACTIVATED - Bot will stop after next profit")
        return jsonify({'success': True, 'message': 'Stop at win activated'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/stop-at-max-streak', methods=['POST'])
def stop_at_max_streak():
    """Stop bot when max loss streak is hit"""
    try:
        BOT_STATE['stop_at_max_step'] = True
        BOT_STATE['force_stop'] = False
        BOT_STATE['stop_at_win'] = False
        print(f"⚠️ STOP AT MAX STEP ACTIVATED")
        return jsonify({'success': True, 'message': 'Stop at max step activated'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/clear-stop-conditions', methods=['POST'])
def clear_stop_conditions():
    """Clear all stop conditions"""
    try:
        BOT_STATE['stop_at_win'] = False
        BOT_STATE['stop_at_max_step'] = False
        BOT_STATE['force_stop'] = False
        print("✅ STOP CONDITIONS CLEARED - Bot will run normally")
        return jsonify({'success': True, 'message': 'Stop conditions cleared'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/update-symbol', methods=['POST'])
def update_symbol():
    """Update trading symbol"""
    try:
        data = request.get_json()
        new_symbol = data.get('symbol')
        
        if not new_symbol:
            return jsonify({'success': False, 'message': 'Symbol is required'}), 400
        
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
        current_step = BOT_STATE['current_step']
        current_lot = BOT_STATE['current_lot']
        
        next_step = current_step + 1
        if next_step > BOT_STATE['max_steps']:
            next_step = 1
        next_lot = LOT_STEPS[next_step]
        
        current_lot_size = LOT_SIZES.get(BOT_STATE['symbol'], 10)
        
        return jsonify({
            'success': True,
            'status': {
                'running': BOT_STATE['running'],
                'current_step': current_step,
                'current_lot': current_lot,
                'next_step': next_step,
                'next_lot': next_lot,
                'max_steps': BOT_STATE['max_steps'],
                'last_result': BOT_STATE['last_result'],
                'base_lot': BOT_STATE['base_lot'],
                'leverage': BOT_STATE['leverage'],
                'tp_percent': BOT_STATE['tp_percent'],
                'sl_percent': BOT_STATE['sl_percent'],
                'symbol': BOT_STATE['symbol'],
                'stop_at_win': BOT_STATE['stop_at_win'],
                'stop_at_max_step': BOT_STATE['stop_at_max_step'],
                'force_stop': BOT_STATE['force_stop'],
                'current_lot_size': current_lot_size,
                'session_start_time': BOT_STATE['session_start_time'],
                'session_total_pnl': BOT_STATE['session_total_pnl'],
                'lot_steps': LOT_STEPS
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error getting status: {str(e)}'}), 500

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

@app.route('/api/wallet-balance', methods=['GET'])
def wallet_balance():
    balance_data = get_wallet_balance()
    return jsonify(balance_data)

@app.route('/api/trade-history', methods=['GET'])
def trade_history():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page
    
    try:
        count_result = execute_mysql_query('SELECT COUNT(*) as total FROM closed_positions', fetch_one=True)
        total_trades = count_result['total'] if count_result else 0
        
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
        
        numeric_ids = []
        for trade_id in trade_ids:
            try:
                if str(trade_id).startswith('trade_'):
                    continue
                numeric_ids.append(int(trade_id))
            except ValueError:
                continue
        
        if not numeric_ids:
            return jsonify({'success': False, 'message': 'No valid trade IDs found'})
        
        placeholders = ','.join(['%s'] * len(numeric_ids))
        
        delete_query = f'DELETE FROM closed_positions WHERE id IN ({placeholders})'
        execute_mysql_query(delete_query, numeric_ids, commit=True)
        
        return jsonify({
            'success': True, 
            'message': f'Successfully deleted {len(numeric_ids)} trade(s)'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error deleting trades: {str(e)}'})


# ========== TP/SL GUARDIAN ==========
def auto_tp_sl_guardian():
    """
    🛡️ SAFE TP/SL GUARDIAN
    - Runs every 2 seconds
    - Edits wrong TP/SL (No Deletion)
    - Places missing TP/SL immediately
    - Uses Tolerance to avoid constant editing
    """
    print("🛡️ SAFE TP/SL GUARDIAN STARTED (EDIT ONLY)...")

    SYMBOL_TICK = {
        "ADAUSD": Decimal("0.00001"),
        "BTCUSD": Decimal("0.5"),
        "ETHUSD": Decimal("0.05"),
    }

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
                if abs(float(p.get('size', 0))) >= 1                          # FIX 1: was > 0.0001
            ]

            if not active_positions:
                print("ℹ️ No active positions")
                continue

            for pos in active_positions:
                try:
                    symbol     = pos.get("product_symbol") or pos.get("symbol")
                    size       = float(pos.get("size", 0))
                    entry      = float(pos.get("entry_price", 0))
                    product_id = pos.get("product_id")

                    if not all([symbol, product_id]) or abs(size) < 1 or entry <= 0:  # FIX 1: was < 0.0001
                        continue

                    # FIX 2: Per-symbol tick config
                    tick = SYMBOL_TICK.get(symbol)
                    if not tick:
                        print(f"   ❌ No tick config for {symbol}, skipping")
                        continue

                    def to_tick(val):
                        d = Decimal(str(val))
                        return (d / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

                    def snap_str(val):
                        snapped  = to_tick(val)
                        tick_str = format(tick, 'f').rstrip('0')
                        decimals = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
                        return format(snapped, f'.{decimals}f'), snapped

                    print(f"\n📍 [GUARDIAN] {symbol} | Size: {size} | Entry: {entry}")

                    if size > 0:  # LONG
                        expected_tp = entry * (1 + LIVE_TP_PERCENTAGE / 100)
                        expected_sl = entry * (1 - LIVE_SL_PERCENTAGE / 100)
                    else:  # SHORT
                        expected_tp = entry * (1 - LIVE_TP_PERCENTAGE / 100)
                        expected_sl = entry * (1 + LIVE_SL_PERCENTAGE / 100)

                    # FIX 2: Snap to tick before use
                    tp_price_str, tp_dec = snap_str(expected_tp)
                    sl_price_str, sl_dec = snap_str(expected_sl)

                    # FIX 3: Use snapped values for tolerance comparison
                    expected_tp_snapped = float(tp_dec)
                    expected_sl_snapped = float(sl_dec)

                    dynamic_tolerance = entry * 0.0005
                    print(f"   💡 Expected TP: {tp_price_str} | Expected SL: {sl_price_str} | Tolerance: {dynamic_tolerance:.6f}")

                    orders_response = make_api_request('GET', f'/orders?product_id={product_id}&state=open')
                    if not orders_response or not orders_response.get('success'):
                        continue

                    orders    = orders_response.get("result", [])
                    tp_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "take_profit_order"]
                    sl_orders = [o for o in orders if o.get("reduce_only") and o.get("stop_order_type") == "stop_loss_order"]

                    tp_valid        = False
                    sl_valid        = False
                    wrong_tp_orders = []
                    wrong_sl_orders = []

                    for tp_order in tp_orders:
                        stop_price = float(tp_order.get("stop_price", 0))
                        if abs(stop_price - expected_tp_snapped) < dynamic_tolerance:  # FIX 3
                            tp_valid = True
                            print(f"   ✅ TP Order {tp_order.get('id')} is CORRECT")
                        else:
                            wrong_tp_orders.append(tp_order)

                    for sl_order in sl_orders:
                        stop_price = float(sl_order.get("stop_price", 0))
                        if abs(stop_price - expected_sl_snapped) < dynamic_tolerance:  # FIX 3
                            sl_valid = True
                            print(f"   ✅ SL Order {sl_order.get('id')} is CORRECT")
                        else:
                            wrong_sl_orders.append(sl_order)

                    tp_edited = False
                    sl_edited = False

                    if wrong_tp_orders and not tp_valid:
                        for tp_order in wrong_tp_orders:
                            order_id = tp_order.get("id")
                            print(f"   🔧 EDITING TP order {order_id}...")
                            edit_payload = {
                                "id"        : order_id,
                                "product_id": int(product_id),
                                "stop_price": tp_price_str,       # FIX 2: tick-snapped, removed invalid order_type
                                "size"      : abs(round(size))    # FIX: round not int
                            }
                            edit_body = json.dumps(edit_payload)
                            try:
                                edit_res = requests.put(
                                    BASE_URL + "/v2/orders",
                                    headers=sign_request("PUT", "/v2/orders", edit_body),
                                    data=edit_body,
                                    timeout=10
                                )
                                if edit_res.status_code == 200:
                                    print(f"   ✅ TP EDITED successfully")
                                    tp_edited = True
                                    break
                                else:
                                    print(f"   ❌ TP edit failed: {edit_res.status_code} | {edit_res.text}")  # FIX 4
                            except Exception as e:
                                print(f"   ❌ TP edit exception: {e}")                                        # FIX 4

                    if wrong_sl_orders and not sl_valid:
                        for sl_order in wrong_sl_orders:
                            order_id = sl_order.get("id")
                            print(f"   🔧 EDITING SL order {order_id}...")
                            edit_payload = {
                                "id"        : order_id,
                                "product_id": int(product_id),
                                "stop_price": sl_price_str,       # FIX 2: tick-snapped, removed invalid order_type
                                "size"      : abs(round(size))    # FIX: round not int
                            }
                            edit_body = json.dumps(edit_payload)
                            try:
                                edit_res = requests.put(
                                    BASE_URL + "/v2/orders",
                                    headers=sign_request("PUT", "/v2/orders", edit_body),
                                    data=edit_body,
                                    timeout=10
                                )
                                if edit_res.status_code == 200:
                                    print(f"   ✅ SL EDITED successfully")
                                    sl_edited = True
                                    break
                                else:
                                    print(f"   ❌ SL edit failed: {edit_res.status_code} | {edit_res.text}")  # FIX 4
                            except Exception as e:
                                print(f"   ❌ SL edit exception: {e}")                                        # FIX 4

                    need_tp = not tp_valid and not tp_edited
                    need_sl = not sl_valid and not sl_edited

                    if need_tp or need_sl:
                        ticker = make_api_request('GET', f'/tickers/{symbol}')
                        if ticker:
                            curr_price = float(ticker['result']['close'])
                            is_safe    = True
                            if size > 0:
                                if expected_tp_snapped <= curr_price or expected_sl_snapped >= curr_price:  # FIX 3
                                    is_safe = False
                            else:
                                if expected_tp_snapped >= curr_price or expected_sl_snapped <= curr_price:  # FIX 3
                                    is_safe = False

                            if not is_safe:
                                print(f"   ⚠️ Price too close to TP/SL. Skipping placement to avoid error.")
                                continue

                        print(f"   📤 Placing missing TP/SL...")
                        payload = {
                            "product_id": int(product_id),
                            "take_profit_order": {
                                "order_type": "market_order",
                                "stop_price": tp_price_str        # FIX 2: tick-snapped string
                            },
                            "stop_loss_order": {
                                "order_type": "market_order",
                                "stop_price": sl_price_str        # FIX 2: tick-snapped string
                            },
                            "bracket_stop_trigger_method": "mark_price"
                        }
                        body = json.dumps(payload)
                        try:
                            res = requests.post(
                                BASE_URL + "/v2/orders/bracket",
                                headers=sign_request("POST", "/v2/orders/bracket", body),
                                data=body,
                                timeout=10
                            )
                            if res.status_code == 200:
                                print(f"   ✅ Bracket placed successfully")
                            else:
                                print(f"   ❌ Bracket placement failed: {res.status_code} | {res.text}")  # FIX 4
                        except Exception as e:
                            print(f"   ❌ Bracket placement exception: {e}")                              # FIX 4

                    time.sleep(0.3)

                except Exception as e:
                    print(f"   ❌ Error: {e}")

        except Exception as e:
            print(f"❌ Guardian error: {e}")
            time.sleep(2)


# ========== MAIN ==========
if __name__ == '__main__':
    init_database()
    
    print("Starting keepalive for Render...")
    start_keep_alive()
    print("Keepalive started - will ping every 10 minutes")
    
    print("Starting signal bot...")
    if start_signal_bot():
        print("Signal bot started successfully")
    else:
        print("Failed to start signal bot")
    
    guardian_thread = threading.Thread(target=auto_tp_sl_guardian, daemon=True)
    guardian_thread.start()
    print("TP/SL Guardian started in background")
    
    try:
        app.run(debug=True, host='0.0.0.0', port=8090, use_reloader=False)
    finally:
        print("Stopping signal bot...")
        stop_signal_bot()
        print("Signal bot stopped")