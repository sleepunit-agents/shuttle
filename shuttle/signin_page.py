"""The sign-in pages and actions (shuttle/signin.py) for a loom piece's page:
one sign-in under way at signin/<id>, and the two actions that start one and
hand it the pasted code. loom-shuttle (Bridges -> Sign in) and
loom-subscriptions (a subscription's own sign-in) both use them."""
from shuttle import signin


def progress_page(sid, back="signin"):
    """signin/<id>: where the sign-in is, and what to do next."""
    st = signin.read(sid)
    if not st:
        return {"title": "Not found", "sections": []}
    h = signin.HARNESSES[st["harness"]]
    blocks = []
    status = st.get("status")
    if status == "starting":
        blocks.append({"type": "text", "text": "Starting the sign-in; reload in a moment.", "tone": "muted"})
    elif status == "checking":
        blocks.append({"type": "text", "text": f"Checking the code with {h['label']} (it can take a minute or two); reload to see.", "tone": "muted"})
    elif status == "waiting":
        blocks.append({"type": "link", "text": f"Sign in to {h['label']}", "href": st["link"]})
        if h["kind"] in ("paste-code", "paste-login"):
            blocks.append({"type": "form", "action": "signin-code", "submit": "Finish", "hidden": {"id": sid},
                           "note": "Paste the code the sign-in page shows.",
                           "fields": [{"name": "code", "label": "Code", "kind": "secret", "required": True}]})
        else:
            if st.get("user_code"):
                blocks.append({"type": "kv", "items": [{"label": "Code to enter", "value": st["user_code"]}]})
            blocks.append({"type": "text", "text": "This finishes by itself once you've signed in; reload to see.", "tone": "muted"})
    elif status == "done":
        where = f" for {st['subscription']}" if st.get("subscription") else ""
        if st.get("secret"):
            blocks.append({"type": "callout", "tone": "ok", "text": f"Signed in{where}. The token is in the secrets store as {st['secret']}; houses started from now on can use it."})
        else:
            blocks.append({"type": "callout", "tone": "ok", "text": f"Signed in{where}. This machine's claude keeps the login and refreshes it."})
        blocks.append({"type": "link", "text": "Back", "page": back})
    else:
        blocks.append({"type": "callout", "tone": "bad", "text": st.get("error") or "It didn't work."})
        blocks.append({"type": "link", "text": "Start again", "page": back})
    return {"title": f"Sign in to {h['label']}", "sections": [{"id": "signin", "title": "Sign-in", "blocks": blocks}]}


def start_act(harness, secret=None, subscription=None, back="signin"):
    if harness not in signin.HARNESSES:
        return {"ok": False, "message": "No harness by that name."}
    try:
        sid, st = signin.start(harness, secret=secret, subscription=subscription)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    if st.get("status") == "failed":
        return {"ok": False, "message": st.get("error") or "The sign-in didn't start.", "page": back}
    return {"ok": True, "message": "Open the link, sign in, then come back here.", "page": f"signin/{sid}"}


def code_act(fields, back="signin"):
    sid = fields.get("id") or ""
    st = signin.submit_code(sid, fields.get("code"))
    if not st:
        return {"ok": False, "message": "That sign-in is gone; start again.", "page": back}
    if st.get("status") == "done":
        return {"ok": True, "message": f"Signed in: {st['secret']} is in the secrets store." if st.get("secret") else "Signed in.", "page": f"signin/{sid}"}
    if st.get("status") == "failed":
        return {"ok": False, "message": st.get("error") or "It didn't work.", "page": f"signin/{sid}"}
    return {"ok": True, "message": st.get("error") or "Checking the code; reload in a moment.", "page": f"signin/{sid}"}
