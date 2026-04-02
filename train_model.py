import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout
import joblib

def train_gold_ai():
    print("Checking MT5 connection...")
    if not mt5.initialize():
        print(f"MT5 Initialize failed! Error: {mt5.last_error()}")
        return
    
    print("Connection successful. Downloading data...")
    # Make sure XAUUSD is in your Market Watch
    symbol = "XAUUSD" 
    mt5.symbol_select(symbol, True)
    
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 20000)  # Get last 20,000 minutes of data
    
    if rates is None or len(rates) == 0:
        print(f"No data found for {symbol}. Check if the symbol name is correct in MT5 (e.g., GOLD vs XAUUSD).")
        mt5.shutdown()
        return

    print(f"Downloaded {len(rates)} candles. Processing...")
    df = pd.DataFrame(rates)
    data = df['close'].values.reshape(-1, 1)

    scaler = MinMaxScaler(feature_range=(0, 1))
    scaled_data = scaler.fit_transform(data)
    joblib.dump(scaler, 'scaler.pkl')

    X, y = [], []
    for i in range(60, len(scaled_data)):
        X.append(scaled_data[i-60:i, 0])
        y.append(scaled_data[i, 0])
    X, y = np.array(X), np.array(y)
    X = np.reshape(X, (X.shape[0], X.shape[1], 1))

    print("Building and training LSTM (this will take a few minutes)...")
    model = Sequential([
        LSTM(50, return_sequences=True, input_shape=(60, 1)),
        Dropout(0.2),
        LSTM(50, return_sequences=False),
        Dense(1)
    ])
    model.compile(optimizer='adam', loss='mse')
    
    model.fit(X, y, epochs=5, batch_size=32, verbose=1)
    model.save('gold_model.keras')  # Instead of .h5
    print("--- SUCCESS: Model and Scaler Saved! ---")
    mt5.shutdown()

if __name__ == "__main__":
    train_gold_ai()