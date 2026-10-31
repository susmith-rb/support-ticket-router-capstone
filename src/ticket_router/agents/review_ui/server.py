"""Server-rendered minimal HTML Review UI for human reps behind Cloud Run IAP.

# REQ-AGENT-034: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ticket_router.agents.review_ui.auth import get_current_rep
from ticket_router.agents.review_ui.client import HumanQueueClient

logger = logging.getLogger("ticket_router.agents.review_ui.server")

_BASE_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    color: #202124;
    background-color: #f8f9fa;
    margin: 0;
    padding: 0;
}
.header {
    background-color: #1a73e8;
    color: #ffffff;
    padding: 1rem 2rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.header h1 {
    margin: 0;
    font-size: 1.25rem;
}
.header-rep {
    font-size: 0.9rem;
    background: rgba(255, 255, 255, 0.2);
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
}
.container {
    max-width: 1100px;
    margin: 2rem auto;
    padding: 0 1rem;
}
.card {
    background: #ffffff;
    border: 1px solid #dadce0;
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 1px 2px rgba(60,64,67,0.1);
}
.card h2 {
    margin-top: 0;
    font-size: 1.15rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.5rem;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 1rem;
}
th, td {
    text-align: left;
    padding: 0.75rem;
    border-bottom: 1px solid #dadce0;
}
th {
    background-color: #f1f3f4;
    font-size: 0.85rem;
    text-transform: uppercase;
    color: #5f6368;
}
tr:hover {
    background-color: #f8f9fa;
}
.badge {
    display: inline-block;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    font-weight: 600;
}
.badge-urgent { background: #fce8e6; color: #c5221f; }
.badge-standard { background: #e8f0fe; color: #1a73e8; }
.badge-open { background: #e6f4ea; color: #137333; }
.badge-assigned { background: #fef7e0; color: #b06000; }
.badge-resolved { background: #f1f3f4; color: #5f6368; }
.btn {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 4px;
    font-weight: 500;
    font-size: 0.9rem;
    cursor: pointer;
    text-decoration: none;
    border: 1px solid transparent;
}
.btn-primary { background: #1a73e8; color: #fff; }
.btn-primary:hover { background: #1557b0; }
.btn-warning { background: #f29900; color: #fff; }
.btn-warning:hover { background: #d08400; }
.btn-success { background: #1e8e3e; color: #fff; }
.btn-success:hover { background: #137333; }
.btn-secondary { background: #f1f3f4; color: #3c4043; border-color: #dadce0; }
.btn-secondary:hover { background: #e8eaed; }
.alert {
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 1rem;
}
.alert-danger { background: #fce8e6; color: #c5221f; border: 1px solid #fad2cf; }
.alert-success { background: #e6f4ea; color: #137333; border: 1px solid #ceead6; }
.alert-info { background: #e8f0fe; color: #1a73e8; border: 1px solid #d2e3fc; }
.message-box {
    background: #f8f9fa;
    border: 1px solid #dadce0;
    border-left: 4px solid #1a73e8;
    padding: 1rem;
    border-radius: 4px;
    font-family: inherit;
    white-space: pre-wrap;
}
.pii-box {
    background: #fef7e0;
    border: 1px solid #f9ab00;
    border-left: 4px solid #ea8600;
    padding: 1rem;
    border-radius: 4px;
    margin-top: 1rem;
}
.filters {
    margin-bottom: 1rem;
    display: flex;
    gap: 0.5rem;
}
textarea {
    width: 100%;
    min-height: 80px;
    padding: 0.5rem;
    border: 1px solid #dadce0;
    border-radius: 4px;
    margin-bottom: 0.5rem;
    box-sizing: border-box;
}
"""


def _render_layout(title: str, content: str, rep_id: str) -> str:
    escaped_title = html.escape(title)
    escaped_rep = html.escape(rep_id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escaped_title} - Human Review UI</title>
    <style>{_BASE_CSS}</style>
</head>
<body>
    <header class="header">
        <h1>{escaped_title}</h1>
        <div class="header-rep">Rep: <strong>{escaped_rep}</strong></div>
    </header>
    <main class="container">
        {content}
    </main>
</body>
</html>
"""


def get_queue_client(request: Request) -> HumanQueueClient:
    """Dependency returning configured HumanQueueClient from app.state."""
    client = getattr(request.app.state, "queue_client", None)
    if client is None:
        client = HumanQueueClient()
        request.app.state.queue_client = client
    return client


def create_app(
    queue_client: Optional[HumanQueueClient] = None,
    iap_verifier: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
    expected_audience: Optional[str] = None,
) -> FastAPI:
    """Create FastAPI application for human review web UI."""
    app = FastAPI(
        title="human-review-ui",
        version="0.1.0",
        description="Human reviewer portal behind Cloud Run IAP with verified rep identity.",
    )
    app.state.queue_client = queue_client
    app.state.iap_verifier = iap_verifier
    app.state.expected_audience = expected_audience

    @app.get("/health")
    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def queue_list_page(
        queue: Optional[str] = Query(None, description="Optional queue filter"),
        rep_id: str = Depends(get_current_rep),
        client: HumanQueueClient = Depends(get_queue_client),
    ) -> HTMLResponse:
        """Render open ticket queue list."""
        try:
            tickets = client.list_queue(rep_id=rep_id, queue=queue)
        except Exception as exc:
            logger.error("Failed to list queue from human-queue: %s", exc)
            tickets = []

        # Filter navigation
        current_q = queue or "all"
        filter_html = f"""
        <div class="filters">
            <a class="btn {'btn-primary' if current_q == 'all' else 'btn-secondary'}" href="/">All Open</a>
            <a class="btn {'btn-primary' if current_q == 'human-urgent' else 'btn-secondary'}" href="/?queue=human-urgent">Urgent (P1)</a>
            <a class="btn {'btn-primary' if current_q == 'human-standard' else 'btn-secondary'}" href="/?queue=human-standard">Standard (P2)</a>
        </div>
        """

        # Table rows
        if tickets:
            rows = []
            for t in tickets:
                t_id = html.escape(str(t.get("ticket_id", "")))
                q_name = str(t.get("assigned_queue", "human-standard"))
                q_badge = "badge-urgent" if q_name == "human-urgent" else "badge-standard"
                q_display = "Urgent (P1)" if q_name == "human-urgent" else "Standard (P2)"
                st_name = str(t.get("status", "open"))
                st_badge = f"badge-{st_name}"
                assigned = html.escape(str(t.get("assigned_to") or "Unassigned"))
                redacted_snip = html.escape(str(t.get("body_redacted", ""))[:120])
                created = html.escape(str(t.get("created_at", "")))

                rows.append(f"""
                <tr>
                    <td><span class="badge {q_badge}">{q_display}</span></td>
                    <td><a href="/tickets/{t_id}"><strong>{t_id[:8]}...</strong></a></td>
                    <td><span class="badge {st_badge}">{html.escape(st_name)}</span></td>
                    <td>{assigned}</td>
                    <td>{redacted_snip}</td>
                    <td><small>{created}</small></td>
                    <td><a class="btn btn-secondary" href="/tickets/{t_id}">View</a></td>
                </tr>
                """)
            table_body = "\n".join(rows)
            content = f"""
            <div class="card">
                <h2>Escalated Tickets Queue</h2>
                {filter_html}
                <table>
                    <thead>
                        <tr>
                            <th>Priority</th>
                            <th>Ticket ID</th>
                            <th>Status</th>
                            <th>Assigned To</th>
                            <th>Customer Message (Redacted)</th>
                            <th>Created</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_body}
                    </tbody>
                </table>
            </div>
            """
        else:
            content = f"""
            <div class="card">
                <h2>Escalated Tickets Queue</h2>
                {filter_html}
                <div class="alert alert-info">No escalated tickets in this queue.</div>
            </div>
            """

        page = _render_layout(title="Review Queue", content=content, rep_id=rep_id)
        return HTMLResponse(content=page)

    @app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    def ticket_detail_page(
        ticket_id: str,
        rep_id: str = Depends(get_current_rep),
        client: HumanQueueClient = Depends(get_queue_client),
        unmasked_json: Optional[str] = Query(None, description="Internal unmasked fields state"),
        error_msg: Optional[str] = Query(None, description="Optional error message banner"),
    ) -> HTMLResponse:
        """Render ticket detail page with redacted body, event history, and action controls."""
        try:
            detail = client.get_ticket(ticket_id=ticket_id, rep_id=rep_id)
        except Exception as exc:
            logger.error("Failed to fetch ticket %s: %s", ticket_id, exc)
            raise HTTPException(status_code=404, detail="Ticket not found") from exc

        ticket = detail.get("ticket", {})
        events = detail.get("events", [])

        t_id = html.escape(str(ticket.get("ticket_id", ticket_id)))
        q_name = str(ticket.get("assigned_queue", "human-standard"))
        q_badge = "badge-urgent" if q_name == "human-urgent" else "badge-standard"
        q_display = "Urgent (P1)" if q_name == "human-urgent" else "Standard (P2)"
        st_name = str(ticket.get("status", "open"))
        st_badge = f"badge-{st_name}"
        assigned = str(ticket.get("assigned_to") or "")
        assigned_display = html.escape(assigned if assigned else "Unassigned")
        body_redacted = html.escape(str(ticket.get("body_redacted", "")))
        customer_id = html.escape(str(ticket.get("customer_id", "")))
        created = html.escape(str(ticket.get("created_at", "")))

        actual_unmasked = unmasked_json if isinstance(unmasked_json, str) else None
        actual_error = error_msg if isinstance(error_msg, str) else None

        error_banner = ""
        if actual_error:
            error_banner = f'<div class="alert alert-danger">{html.escape(actual_error)}</div>'

        # PII unmasking display
        unmasked_html = ""
        if actual_unmasked:
            try:
                unmasked_data = json.loads(actual_unmasked)
                fields_rows = "\n".join(
                    f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
                    for k, v in unmasked_data.items()
                )
                unmasked_html = f"""
                <div class="pii-box">
                    <h3 style="margin-top:0; color:#b06000;">⚠️ Restored Customer PII (Audited)</h3>
                    <p style="font-size:0.85rem; color:#5f6368;">Visible exclusively to claiming rep: <strong>{html.escape(rep_id)}</strong>.</p>
                    <table><tbody>{fields_rows}</tbody></table>
                </div>
                """
            except Exception:
                pass
        else:
            unmasked_html = """
            <div class="alert alert-info" style="margin-top:1rem;">
                Customer PII is currently redacted and encrypted. Restoring PII is restricted to the claiming rep and is recorded in immutable audit events.
            </div>
            """

        # Action forms
        actions_html = ""
        is_claimed_by_me = assigned == rep_id
        if st_name != "resolved":
            if not is_claimed_by_me:
                actions_html = f"""
                <form method="post" action="/tickets/{t_id}/claim">
                    <button type="submit" class="btn btn-primary">Claim Ticket</button>
                </form>
                """
            else:
                actions_html = f"""
                <div style="display:flex; gap:1rem; flex-wrap:wrap; align-items:flex-start;">
                    <form method="post" action="/tickets/{t_id}/reidentify">
                        <button type="submit" class="btn btn-warning">Restore PII (Audited)</button>
                    </form>
                    <form method="post" action="/tickets/{t_id}/resolve" style="flex:1; min-width:300px;">
                        <textarea name="resolution_notes" placeholder="Enter resolution notes..." required></textarea>
                        <button type="submit" class="btn btn-success">Resolve Ticket</button>
                    </form>
                </div>
                """
        else:
            actions_html = '<div class="alert alert-success">Ticket is resolved.</div>'

        # Events table
        event_rows = []
        for ev in events:
            ev_ts = html.escape(str(ev.get("created_at", "")))
            ev_type = html.escape(str(ev.get("event_type", "")))
            ev_actor = html.escape(str(ev.get("actor", "")))
            ev_payload = html.escape(json.dumps(ev.get("payload", {})))
            event_rows.append(f"""
            <tr>
                <td><small>{ev_ts}</small></td>
                <td><strong>{ev_type}</strong></td>
                <td>{ev_actor}</td>
                <td><code>{ev_payload}</code></td>
            </tr>
            """)
        events_table_body = "\n".join(event_rows) if event_rows else "<tr><td colspan='4'>No events recorded.</td></tr>"

        content = f"""
        <p><a href="/" class="btn btn-secondary">&larr; Back to Queue</a></p>
        {error_banner}
        <div class="card">
            <h2>Ticket {t_id}</h2>
            <table>
                <tbody>
                    <tr><th>Priority</th><td><span class="badge {q_badge}">{q_display}</span></td></tr>
                    <tr><th>Status</th><td><span class="badge {st_badge}">{html.escape(st_name)}</span></td></tr>
                    <tr><th>Assigned To</th><td><strong>{assigned_display}</strong></td></tr>
                    <tr><th>Customer ID</th><td>{customer_id}</td></tr>
                    <tr><th>Created At</th><td>{created}</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Customer Message (Redacted)</h2>
            <div class="message-box">{body_redacted}</div>
            {unmasked_html}
        </div>

        <div class="card">
            <h2>Actions</h2>
            {actions_html}
        </div>

        <div class="card">
            <h2>Audit &amp; Event History</h2>
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Event Type</th>
                        <th>Actor</th>
                        <th>Payload</th>
                    </tr>
                </thead>
                <tbody>
                    {events_table_body}
                </tbody>
            </table>
        </div>
        """

        page = _render_layout(title=f"Ticket {t_id[:8]}", content=content, rep_id=rep_id)
        return HTMLResponse(content=page)

    @app.post("/tickets/{ticket_id}/claim", response_class=HTMLResponse)
    def claim_ticket_action(
        ticket_id: str,
        rep_id: str = Depends(get_current_rep),
        client: HumanQueueClient = Depends(get_queue_client),
    ) -> RedirectResponse:
        """Claim ticket for the authenticated rep."""
        try:
            client.claim_ticket(ticket_id=ticket_id, rep_id=rep_id)
            return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)
        except Exception as exc:
            logger.warning("Claim failed for ticket %s by %s: %s", ticket_id, rep_id, exc)
            return RedirectResponse(
                url=f"/tickets/{ticket_id}?error_msg=Failed+to+claim+ticket",
                status_code=303,
            )

    @app.post("/tickets/{ticket_id}/reidentify", response_class=HTMLResponse)
    def reidentify_action(
        ticket_id: str,
        rep_id: str = Depends(get_current_rep),
        client: HumanQueueClient = Depends(get_queue_client),
    ) -> HTMLResponse:
        """Unmask customer PII for claiming rep (audited)."""
        try:
            result = client.reidentify_pii(ticket_id=ticket_id, rep_id=rep_id)
            unmasked = result.get("unmasked_fields", {})
            # Re-render ticket detail page displaying unmasked fields
            return ticket_detail_page(
                ticket_id=ticket_id,
                rep_id=rep_id,
                client=client,
                unmasked_json=json.dumps(unmasked),
                error_msg=None,
            )
        except Exception as exc:
            logger.warning("Reidentify failed for ticket %s by %s: %s", ticket_id, rep_id, exc)
            return ticket_detail_page(
                ticket_id=ticket_id,
                rep_id=rep_id,
                client=client,
                unmasked_json=None,
                error_msg="Access Denied: Ticket must be claimed by caller before unmasking PII.",
            )

    @app.post("/tickets/{ticket_id}/resolve", response_class=HTMLResponse)
    async def resolve_ticket_action(
        ticket_id: str,
        request: Request,
        rep_id: str = Depends(get_current_rep),
        client: HumanQueueClient = Depends(get_queue_client),
    ) -> RedirectResponse:
        """Resolve ticket with notes."""
        body_bytes = await request.body()
        form_data = parse_qs(body_bytes.decode("utf-8"))
        resolution_notes = form_data.get("resolution_notes", [""])[0]
        try:
            client.resolve_ticket(
                ticket_id=ticket_id,
                rep_id=rep_id,
                resolution_notes=resolution_notes,
            )
            return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)
        except Exception as exc:
            logger.warning("Resolve failed for ticket %s: %s", ticket_id, exc)
            return RedirectResponse(
                url=f"/tickets/{ticket_id}?error_msg=Failed+to+resolve+ticket",
                status_code=303,
            )

    return app
