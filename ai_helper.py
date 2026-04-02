import numpy as np
import joblib
from tensorflow.keras.models import load_model

class GoldAI:
# In ai_helper.py, update the __init__ method
    def __init__(self):
        # Load the new .keras file and tell it not to worry about the 'mse' training settings
        self.model = load_model('gold_model.keras', compile=False) 
        self.scaler = joblib.load('scaler.pkl')
        self.seq_len = 60
        
    def get_prediction_with_raw(self, df):
        if len(df) < self.seq_len: return None, 0
        
        last_60 = df['close'].values[-self.seq_len:].reshape(-1, 1)
        scaled_seq = self.scaler.transform(last_60)
        X = np.reshape(scaled_seq, (1, self.seq_len, 1))
        
        pred_scaled = self.model.predict(X, verbose=0)
        pred_price = self.scaler.inverse_transform(pred_scaled)[0][0]
        current_price = df['close'].iloc[-1]
        
        diff = pred_price - current_price
        signal = None
        if diff > 0.15: signal = "BUY"
        elif diff < -0.15: signal = "SELL"
        
        return signal, pred_price