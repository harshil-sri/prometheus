"""
lstm_model.py

LSTM-based sequence modeling branch for detecting burst anomalies and
temporal fraud patterns in transaction sequences.
"""

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from collections import defaultdict


class LSTMFraudDetector(nn.Module):
    def __init__(self, input_dim=20, hidden_dim=32, num_layers=2, dropout=0.2, seed=42):
        super().__init__()
        torch.manual_seed(seed)
        self.lstm = nn.LSTM(
            input_size=input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0
        )
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        # x shape: (batch, seq_len, features)
        out, (hn, cn) = self.lstm(x)
        # Take the last output in the sequence
        last_out = out[:, -1, :]
        return self.fc(last_out).squeeze(-1)


class LSTMWrapper:
    """Wrapper to integrate LSTM into the BlueTeamEnsemble."""

    def __init__(self, input_dim=20, seq_len=10, epochs=3, batch_size=256, seed=42):
        self.input_dim = input_dim
        self.seq_len = seq_len
        self.epochs = epochs
        self.batch_size = batch_size
        self.seed = seed
        self.model = LSTMFraudDetector(input_dim=input_dim, seed=seed)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=0.001)
        self.criterion = nn.BCELoss()

    def _build_sequences(self, X, transactions):
        """Builds (seq_len, input_dim) sequences per transaction based on sender history."""
        # Group by sender
        txs_by_sender = defaultdict(list)
        for idx, tx in enumerate(transactions):
            txs_by_sender[tx['from']].append((idx, X[idx]))

        seq_X = np.zeros((len(X), self.seq_len, self.input_dim))
        
        for sender, tx_list in txs_by_sender.items():
            # Sort by transaction index (chronological as per log)
            tx_list.sort(key=lambda x: x[0])
            history = []
            for tx_idx, features in tx_list:
                history.append(features)
                # Take up to seq_len recent transactions
                recent_history = history[-self.seq_len:]
                
                # Pad if history is shorter than seq_len
                if len(recent_history) < self.seq_len:
                    padding = [np.zeros(self.input_dim)] * (self.seq_len - len(recent_history))
                    padded_seq = padding + recent_history
                else:
                    padded_seq = recent_history
                    
                seq_X[tx_idx] = np.array(padded_seq)
                
        return torch.tensor(seq_X, dtype=torch.float32)

    def fit(self, X, y, transactions):
        """Train the LSTM model."""
        if len(X) == 0:
            return self

        self.model.train()
        seq_X = self._build_sequences(X, transactions)
        t_y = torch.tensor(y, dtype=torch.float32)

        dataset = TensorDataset(seq_X, t_y)
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)

        for epoch in range(self.epochs):
            total_loss = 0
            for batch_X, batch_y in loader:
                batch_X, batch_y = batch_X.to(self.device), batch_y.to(self.device)
                self.optimizer.zero_grad()
                preds = self.model(batch_X)
                loss = self.criterion(preds, batch_y)
                loss.backward()
                self.optimizer.step()
                total_loss += loss.item()
                
        return self

    def predict_proba(self, X, transactions):
        """Predict probabilities using the trained LSTM."""
        if len(X) == 0:
            return np.array([])
            
        self.model.eval()
        seq_X = self._build_sequences(X, transactions)
        dataset = TensorDataset(seq_X)
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False)
        
        probs = []
        with torch.no_grad():
            for (batch_X,) in loader:
                batch_X = batch_X.to(self.device)
                preds = self.model(batch_X)
                probs.extend(preds.cpu().numpy().tolist())
                
        return np.array(probs)
