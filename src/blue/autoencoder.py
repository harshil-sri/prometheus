"""
autoencoder.py

Autoencoder-based anomaly detection branch for detecting out-of-distribution
fraud without requiring labels.
"""

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

class Autoencoder(nn.Module):
    def __init__(self, input_dim=20, hidden_dim=8, seed=42):
        super().__init__()
        torch.manual_seed(seed)
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 16),
            nn.ReLU(),
            nn.Linear(16, hidden_dim),
            nn.ReLU()
        )
        self.decoder = nn.Sequential(
            nn.Linear(hidden_dim, 16),
            nn.ReLU(),
            nn.Linear(16, input_dim)
        )

    def forward(self, x):
        encoded = self.encoder(x)
        decoded = self.decoder(encoded)
        return decoded

class AutoencoderFraudDetector:
    """Wrapper to integrate Autoencoder into BlueTeamEnsemble."""
    
    def __init__(self, input_dim=20, epochs=5, batch_size=512, seed=42):
        self.input_dim = input_dim
        self.epochs = epochs
        self.batch_size = batch_size
        self.seed = seed
        self.model = Autoencoder(input_dim=input_dim, seed=seed)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=0.005)
        self.criterion = nn.MSELoss(reduction='none')
        # We need a way to scale MSE to a probability [0, 1]
        self.max_mse = 1.0

    def fit(self, X, y=None, feature_names=None):
        """Train the Autoencoder on normal transactions to learn the manifold."""
        if len(X) == 0:
            return self
            
        X = np.asarray(X, dtype=np.float32)
        # In a strict setting, we'd only train on y==0 (normal).
        # But we might only have X and want to train on everything.
        if y is not None:
            # Filter only negative (normal) class for training if labels are provided
            y = np.asarray(y)
            X_train = X[y == 0]
            if len(X_train) == 0:
                X_train = X # fallback to all data if no normal data
        else:
            X_train = X

        t_X = torch.tensor(X_train).to(self.device)
        dataset = TensorDataset(t_X, t_X)
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)

        self.model.train()
        for epoch in range(self.epochs):
            total_loss = 0
            for batch_x, _ in loader:
                self.optimizer.zero_grad()
                reconstructed = self.model(batch_x)
                loss = self.criterion(reconstructed, batch_x).mean()
                loss.backward()
                self.optimizer.step()
                total_loss += loss.item()
                
        # Calibrate max_mse on the training set to scale predictions
        self.model.eval()
        with torch.no_grad():
            reconstructed = self.model(t_X)
            mse = self.criterion(reconstructed, t_X).mean(dim=1).cpu().numpy()
            self.max_mse = np.percentile(mse, 95) if len(mse) > 0 else 1.0
            # Prevent division by zero
            if self.max_mse <= 0.0:
                self.max_mse = 1.0

        return self

    def predict_proba(self, X):
        """Returns normalized anomaly score mapped to [0, 1] probability."""
        if len(X) == 0:
            return np.array([])
            
        X = np.asarray(X, dtype=np.float32)
        t_X = torch.tensor(X).to(self.device)
        self.model.eval()
        
        with torch.no_grad():
            reconstructed = self.model(t_X)
            mse = self.criterion(reconstructed, t_X).mean(dim=1).cpu().numpy()
            
        # Map MSE to a probability-like score (squash through sigmoid-like logic)
        # Scaled so that max_mse -> roughly 0.5 to 0.7
        scaled = mse / self.max_mse
        # Cap at 1.0
        prob = np.clip(scaled, 0.0, 1.0)
        return prob
