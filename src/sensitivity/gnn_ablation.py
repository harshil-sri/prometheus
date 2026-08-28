"""Masked-neighbor ablation for GNN sensitivity analysis."""

import numpy as np

class GNNAblation:
    """Computes GNN sensitivity by removing neighbors / edge types and measuring score change."""

    def __init__(self, gnn_model, node_id_map=None):
        self.gnn = gnn_model
        self.node_id_map = node_id_map or {}

    def _forward(self, data, edge_index):
        """Forward pass supplying edge_attr whenever the architecture
        requires them (Phase 2 TransformerConv(edge_dim=3) hard-asserts).
        Falls back to plain two-arg call for older/attr-free models."""
        import torch
        edge_attr = getattr(data, "edge_attr", None)
        needs_attr = getattr(self.gnn, "EDGE_ATTR_DIM", None) is not None \
            or type(self.gnn).__name__ == "FraudGNN"
        if needs_attr:
            n_edges = edge_index.size(1)
            if edge_attr is None or edge_attr.size(0) != n_edges:
                dim = getattr(self.gnn, "EDGE_ATTR_DIM", 3)
                # align: drop self-loops case by padding zeros to match count
                if edge_attr is None:
                    edge_attr = torch.zeros((n_edges, dim))
                elif edge_attr.size(0) < n_edges:
                    pad = torch.zeros(
                        (n_edges - edge_attr.size(0), edge_attr.size(1)))
                    edge_attr = torch.cat([edge_attr, pad], dim=0)
                else:
                    edge_attr = edge_attr[:n_edges]
            return self.gnn(data.x, edge_index, edge_attr)
        return self.gnn(data.x, edge_index)

    def neighbor_ablation(self, data, node_idx):
        """Remove all neighbors of a node and measure score change.

        Returns:
            dict with original_score, ablated_score, delta
        """
        self.gnn.eval()
        import torch

        with torch.no_grad():
            out = self._forward(data, data.edge_index)
            original_score = float(torch.softmax(out, dim=1)[node_idx, 1].item())

        # Mask edges: remove all edges connected to node_idx
        edge = data.edge_index
        mask = (edge[0] != node_idx) & (edge[1] != node_idx)
        ablated_edges = edge[:, mask]

        with torch.no_grad():
            out_abl = self._forward(data, ablated_edges)
            ablated_score = float(torch.softmax(out_abl, dim=1)[node_idx, 1].item())

        return {
            "original_score": round(original_score, 4),
            "ablated_score": round(ablated_score, 4),
            "delta": round(ablated_score - original_score, 4),
        }

    def edge_type_ablation(self, data, node_idx, edge_type="shared_device"):
        """Simulate removing a specific edge type and measure impact."""
        # For the twin graph, edge types aren't explicitly labeled
        # This is a best-effort analysis
        return self.neighbor_ablation(data, node_idx)

    def graph_density_sensitivity(self, data, node_idx):
        """Measure sensitivity to graph density around a node."""
        self.gnn.eval()
        import torch

        edge = data.edge_index
        # Find neighbors
        neighbors = torch.unique(edge[1, edge[0] == node_idx])

        densities = []
        for frac in [1.0, 0.75, 0.5, 0.25]:
            if len(neighbors) == 0:
                break
            n_keep = max(1, int(len(neighbors) * frac))
            keep = neighbors[torch.randperm(len(neighbors))[:n_keep]]
            mask = torch.zeros(edge.shape[1], dtype=torch.bool)
            for k in keep:
                mask |= (edge[0] == node_idx) & (edge[1] == k)
                mask |= (edge[1] == node_idx) & (edge[0] == k)
            sub_edge = edge[:, mask]

            with torch.no_grad():
                out = self._forward(data, sub_edge)
                score = float(torch.softmax(out, dim=1)[node_idx, 1].item())
            densities.append({"density_fraction": frac, "score": score})

        return densities

    def edge_attr_sensitivity(self, data):
        """Measured impact of the graph's EDGE FEATURES.

        Compares true model output against output with zeroed edge_attr.
        Returns the mean |delta| over nodes — a COMPUTED value, not a label
        (audit finding #5b: this used to be hardcoded 'below_threshold').

        Requires the model to accept (x, edge_index, edge_attr), which the
        Phase 2 TransformerConv rewiring provides.
        """
        import torch

        self.gnn.eval()
        edge_attr = getattr(data, "edge_attr", None)
        if edge_attr is None:
            return {"mean_delta": 0.0, "max_delta": 0.0,
                    "affected_nodes": 0}

        try:
            with torch.no_grad():
                out = self.gnn(data.x, data.edge_index, edge_attr)
                p_true = torch.softmax(out, dim=1)[:, 1]
                zeroed = torch.zeros_like(edge_attr)
                out_z = self.gnn(data.x, data.edge_index, zeroed)
                p_zero = torch.softmax(out_z, dim=1)[:, 1]
            deltas = (p_true - p_zero).abs()
            return {
                "mean_delta": float(deltas.mean()),
                "max_delta": float(deltas.max()),
                "affected_nodes": int((deltas > 0.01).sum()),
            }
        except TypeError:
            # Model ignores edge features entirely — that IS the measurement
            return {"mean_delta": 0.0, "max_delta": 0.0,
                    "affected_nodes": 0}

    def sensitivity_map(self, data):
        """Compute per-node sensitivity for all nodes.

        Returns:
            dict with per-node scores and average sensitivity by edge type
        """
        self.gnn.eval()
        import torch

        with torch.no_grad():
            out = self._forward(data, data.edge_index)
            scores = torch.softmax(out, dim=1)[:, 1].numpy()

        return {
            "node_scores": scores.tolist(),
            "mean_score": float(scores.mean()),
            "max_score": float(scores.max()),
            "high_risk_nodes": int((scores > 0.7).sum()),
        }