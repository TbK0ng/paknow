"""Integrate figures into the graph (plan §6.1): create Figure nodes and
ILLUSTRATED_BY edges from evidence/observation units that reference them.

Figure edges carry ``origin=figure_extract`` so a re-run is idempotent and the
taxonomy rebuild never touches them.
"""
from __future__ import annotations

from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow, FigureAssetRow, GraphEdgeRow, GraphNodeRow
from paknow.graph.contracts import EdgeOrigin
from paknow.ids import stable_id


def link_figures(session_factory=SessionLocal) -> tuple[int, int]:
    """Create figure nodes and ILLUSTRATED_BY edges; returns (nodes, edges)."""
    with session_factory() as s:
        figs = s.query(FigureAssetRow).all()
        n_nodes = 0
        for f in figs:
            nid = stable_id("node", "fig", f.figure_id)
            s.merge(
                GraphNodeRow(
                    node_id=nid,
                    node_type="figure",
                    label=f.figure_label or f.figure_id[:12],
                    meta={"figure_id": f.figure_id, "doc_id": f.doc_id},
                )
            )
            n_nodes += 1

        # evidence/observation units that reference figures
        units = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.figure_ids != "[]").all()
        n_edges = 0
        for u in units:
            for fid in u.figure_ids or []:
                fig_node = stable_id("node", "fig", fid)
                eid = stable_id("edge", u.evidence_id, fig_node, "ill")
                s.merge(
                    GraphEdgeRow(
                        edge_id=eid,
                        from_node=u.evidence_id,
                        to_node=fig_node,
                        relation="ILLUSTRATED_BY",
                        source=u.evidence_id,
                        origin=EdgeOrigin.figure_extract.value,
                    )
                )
                n_edges += 1
        s.commit()
    return n_nodes, n_edges


def main() -> None:
    init_db()
    n_nodes, n_edges = link_figures()
    print(f"figure nodes: {n_nodes}, ILLUSTRATED_BY edges: {n_edges}", flush=True)


if __name__ == "__main__":
    main()
