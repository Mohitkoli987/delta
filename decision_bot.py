"""
decision_bot.py - Pure Random Signal Generator (FIXED)
=======================================================
- Sirf tab naya signal generate karta hai jab position CLOSE ho
- last_exit.json watch karta hai (app.py likhta hai jab TP/SL hit ho)
- True 50/50 random - zero bias
- Har 5 sec overwrite NAHI karta
"""

import random
import json
import time
import os
from datetime import datetime

# Paths
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SIGNAL_FILE = os.path.join(SCRIPT_DIR, 'signal.json')
EXIT_FILE   = os.path.join(SCRIPT_DIR, 'last_exit.json')

def generate_random_signal(reason="random"):
    """Generate completely random unbiased buy/sell signal - TRUE 50/50"""
    signal = random.choice(['BUY', 'SELL'])
    signal_data = {
        'signal':              signal,
        'timestamp':           datetime.now().isoformat(),
        'confidence':          50,
        'layer':               'RANDOM',
        'score':               0,
        'source':              'random_generator',
        'reason':              f'Random unbiased decision after {reason}',
        'decision_ready':      True,
        'decision_confidence': 0.5,
        'position_analysis':   {'has_position': False},
        'backtest_results':    {},
        'last_trade_result':   reason,
    }
    return signal_data

def write_signal(reason="startup"):
    """Atomically write new signal to signal.json"""
    try:
        signal_data = generate_random_signal(reason)
        tmp = SIGNAL_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(signal_data, f, indent=2)
        os.replace(tmp, SIGNAL_FILE)
        print(f"[DECISION-BOT] Signal: {signal_data['signal']} | Reason: {reason}")
        return True
    except Exception as e:
        print(f"[DECISION-BOT] Error writing signal: {e}")
        return False

def read_exit_result():
    """
    last_exit.json padhta hai.
    Returns dict only if new unconsumed exit hai, else None.
    app.py yeh file likhta hai jab TP ya SL hit hota hai.
    """
    try:
        if not os.path.exists(EXIT_FILE):
            return None
        with open(EXIT_FILE, 'r') as f:
            data = json.load(f)
        if data.get('consumed'):
            return None
        return data
    except Exception:
        return None

def mark_exit_consumed():
    """Exit result ko consumed mark karo taaki dobara process na ho"""
    try:
        if not os.path.exists(EXIT_FILE):
            return
        with open(EXIT_FILE, 'r') as f:
            data = json.load(f)
        data['consumed'] = True
        tmp = EXIT_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, EXIT_FILE)
    except Exception as e:
        print(f"[DECISION-BOT] Could not mark exit consumed: {e}")

def main():
    print("[DECISION-BOT] Random Signal Bot Started")
    print("[DECISION-BOT] TRUE 50/50 - Zero Bias - Only generates after position closes")
    print(f"[DECISION-BOT] Signal file : {SIGNAL_FILE}")
    print(f"[DECISION-BOT] Exit file   : {EXIT_FILE}")

    # Startup: ek initial signal likho taaki pehla trade ho sake
    write_signal(reason="startup")

    last_processed_ts = None

    while True:
        try:
            exit_data = read_exit_result()

            if exit_data:
                ts = exit_data.get('timestamp', '')

                if ts != last_processed_ts:
                    exit_type = exit_data.get('exit_type', 'UNKNOWN')
                    pnl       = exit_data.get('pnl', 0)
                    side      = exit_data.get('side', '')

                    print(f"[DECISION-BOT] Exit detected: {exit_type} | PnL={pnl:.5f} | Side={side}")

                    # 100ms wait: position fully settle ho jaye
                    time.sleep(0.1)

                    reason = f"after_{exit_type.lower()}_pnl={pnl:.4f}"
                    write_signal(reason=reason)

                    mark_exit_consumed()
                    last_processed_ts = ts

            # Poll every 100ms
            time.sleep(0.1)

        except KeyboardInterrupt:
            print("[DECISION-BOT] Stopped by user")
            break
        except Exception as e:
            print(f"[DECISION-BOT] Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()