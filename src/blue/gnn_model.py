"""
gnn_model.py — GNN-based fraud detection for Project Prometheus.

Node classification over an account+merchant graph using a 2-layer
GraphSAGE network. CPU-only (no CUDA).
"""

import numpy as np
import torch
import torch.nn.functional as F
from torch_geometric.nn import SAGEConv

__all__ = ["FraudGNN", "GNNFraudDetector"]


class FraudGNN(torch.nn.Module):
    """2-layer GraphSAGE network for binary node classification."""

    def __init__(self, in_channels=16, hidden_channels=64, out_channels=2,
                 dropout=0.5):
        super().__init__()
        self.dropout = dropout
        self.conv1 = SAGEConv(in_channels, hidden_channels)
        self.conv2 = SAGEConv(hidden_channels, out_channels)

    def forward(self, x, edge_index, edge_weight=None):
        """
        Args:
            x:           [num_nodes, in_channels] node feature matrix.
            edge_index:  [2, num_edges] graph connectivity.
            edge_weight: optional [num_edges] edge weights.

        Returns:
            [num_nodes, out_channels] raw logits per node.
        """
        num_nodes = x.size(0)

        # --- Isolated-node handling: append explicit self-loops so every
        # node participates in message passing with normalized weight ---
        loop_index = torch.arange(num_nodes, dtype=torch.long,
                                  device=x.device)
        self_loops = torch.stack([loop_index, loop_index], dim=0)
        edge_index = torch.cat([edge_index, self_loops], dim=1)

        if edge_weight is not None:
            loop_weights = torch.ones(num_nodes, dtype=edge_weight.dtype,
                                      device=edge_weight.device)
            edge_weight = torch.cat([edge_weight, loop_weights], dim=0)

        h = self.conv1(x, edge_index, edge_weight)
        h = F.relu(h)
        h = F.dropout(h, p=self.dropout, training=self.training)
        logits = self.conv2(h, edge_index, edge_weight)
        return logits


class GNNFraudDetector:
    """High-level wrapper: training + inference for fraud risk scoring."""

    def __init__(self, in_channels=16, hidden_channels=64, seed=42):
        self.device = torch.device('cpu')
        torch.manual_seed(seed)
        np.random.seed(seed)

        self.model = FraudGNN(in_channels, hidden_channels)
        self.model.to(self.device)
        self.is_fitted_ = False
        self.history_ = []

    # ------------------------------------------------------------------ #
    def _prepare(self, data):
        """Extract tensors from a PyG Data object; move to CPU device."""
        x = data.x.to(self.device).float()
        edge_index = data.edge_index.to(self.device).long()

        edge_weight = getattr(data, 'edge_weight', None)
        if edge_weight is not None:
            edge_weight = edge_weight.to(self.device).float()

        y = getattr(data, 'y', None)
        if y is not None:
            y = y.to(self.device).long()

        return x, edge_index, edge_weight, y

    @staticmethod
    def _class_weights(y):
        """Inverse-frequency class weights to counter label imbalance."""
        counts = torch.bincount(y, minlength=2).float()
        counts = torch.clamp(counts, min=1.0)          # avoid div-by-zero
        weights = counts.sum() / (2.0 * counts)
        return weights

    # ------------------------------------------------------------------ #
    def fit(self, data, epochs=50, lr=0.01):
        """
        Train the GNN with Adam + weighted cross-entropy.

        Handles empty graphs (no nodes / no edges) gracefully.
        """
        x, edge_index, edge_weight, y = self._prepare(data)

        if x.size(0) == 0:
            raise ValueError("Cannot train: graph contains no nodes.")
        if y is None:
            raise ValueError("Training requires node labels in `data.y`.")

        weights = self._class_weights(y)
        criterion = torch.nn.CrossEntropyLoss(weight=weights)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)

        self.model.train()
        self.history_ = []
        for epoch in range(epochs):
            optimizer.zero_grad()
            logits = self.model(x, edge_index, edge_weight)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            self.history_.append(float(loss.item()))

        self.model.eval()
        self.is_fitted_ = True
        return self

    # ------------------------------------------------------------------ #
    def predict_proba(self, data):
        """
        Returns:
            np.ndarray [num_nodes, 2] — softmax class probabilities;
            column 1 is the positive (fraud) probability.
        """
        x, edge_index, edge_weight, _ = self._prepare(data)

        if x.size(0) == 0:
            return np.zeros((0, 2), dtype=np.float64)

        self.model.eval()
        with torch.no_grad():
            logits = self.model(x, edge_index, edge_weight)
            probs = F.softmax(logits, dim=1)

        return probs.cpu().numpy()

    def predict(self, data):
        """Predicted class label (0 = legit, 1 = fraud) per node."""
        proba = self.predict_proba(data)
        if proba.shape[0] == 0:
            return np.zeros((0,), dtype=np.int64)
        return proba.argmax(axis=1)

    # ------------------------------------------------------------------ #
    def node_risk(self, account_id, data, id_map):
        """
        Fraud risk score for a single account node.

        Args:
            account_id: external account identifier.
            data:       PyG Data object for the full graph.
            id_map:     dict mapping account_id -> node index.

        Returns:
            float in [0, 1] — P(fraud | node).
        """
        if account_id not in id_map:
            raise KeyError(f"Unknown account_id: {account_id!r}")

        node_idx = int(id_map[account_id])
        proba = self.predict_proba(data)

        if node_idx < 0 or node_idx >= proba.shape[0]:
            raise IndexError(
                f"Node index {node_idx} out of range "
                f"(graph has {proba.shape[0]} nodes)."
            )
        return float(proba[node_idx, 1])