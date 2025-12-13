import MetaTrader5 as mt5

def close_all_trades():
    """
    Closes all open trades (positions) on the account.
    Works for both long and short positions.
    """
    if not mt5.initialize():
        print("MT5 Initialization failed")
        return False

    # Get all open positions
    positions = mt5.positions_get()
    if positions is None or len(positions) == 0:
        print("No open positions to close.")
        mt5.shutdown()
        return True

    for pos in positions:
        symbol = pos.symbol
        ticket = pos.ticket
        volume = pos.volume
        position_type = pos.type  # 0 = buy, 1 = sell

        # Determine order type to close
        if position_type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
        else:
            order_type = mt5.ORDER_TYPE_BUY

        # Close order request
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": ticket,
            "price": mt5.symbol_info_tick(symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).bid,
            "deviation": 10,
            "magic": 123456,
            "comment": "Close all trades function",
        }

        result = mt5.order_send(close_request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"Failed to close {symbol}, ticket {ticket}: {result.comment}")
        else:
            print(f"Closed {symbol}, ticket {ticket}")

    mt5.shutdown()
    return True

# Example usage if run directly
if __name__ == "__main__":
    close_all_trades()
