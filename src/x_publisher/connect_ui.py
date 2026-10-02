"""Small, server-rendered UI primitives for the owner connection pages.

Text and attribute values are escaped here. Parameters ending in ``_html`` and
``content`` are trusted HTML fragments assembled by the caller, never user input.
The pages use no JavaScript or inline event handlers. The brand image is a
same-origin asset shipped with the server.
"""

from __future__ import annotations

from html import escape as _html_escape

from .core import PREFIX

BASE = PREFIX + "/connect"
BUFFER = BASE + "/buffer"
CLIENTS = BASE + "/client-settings"
LOGO = BASE + "/assets/x-logo.jpg"


def escape(value: object) -> str:
    """Escape a value for HTML text or a quoted HTML attribute."""
    return _html_escape(str(value), quote=True)


def hidden(name: str, value: object) -> str:
    """Render a hidden form field with escaped name and value."""
    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'


def badge(label: object, tone: str = "neutral") -> str:
    """Render a short account/connection status label.

    Supported tones: success, warning, danger, info, neutral.
    """
    if tone not in {"success", "warning", "danger", "info", "neutral"}:
        tone = "neutral"
    return f'<span class="badge badge--{tone}">{escape(label)}</span>'


def alert(title: object, body: object, tone: str = "info") -> str:
    """Render an accessible alert; title and body are plain text."""
    if tone not in {"success", "warning", "danger", "info"}:
        tone = "info"
    role = ' role="alert"' if tone in {"warning", "danger"} else ' role="status"'
    return (
        f'<div class="alert alert--{tone}"{role}>'
        f'<strong>{escape(title)}</strong><p>{escape(body)}</p></div>'
    )


def card(
    title: object,
    body_html: str,
    *,
    badge_html: str = "",
    actions_html: str = "",
    card_id: str = "",
) -> str:
    """Render a card; only the parameters named ``*_html`` contain trusted markup."""
    id_attr = f' id="{escape(card_id)}"' if card_id else ""
    actions = f'<div class="card__actions">{actions_html}</div>' if actions_html else ""
    return (
        f'<section class="card"{id_attr}>'
        '<div class="card__top"><div>'
        f'<h2>{escape(title)}</h2></div>{badge_html}</div>'
        f'<div class="card__body">{body_html}</div>{actions}</section>'
    )


def owner_key_form(*, return_to: str = "", button_label: str = "Continue") -> str:
    """Render the owner sign-in form for a known destination."""
    if return_to not in {"", "buffer", "client-settings"}:
        raise ValueError("Unknown owner sign-in destination")
    destination = hidden("return_to", return_to) if return_to else ""
    return (
        f'<form class="stack" method="post" action="{BASE}/login">{destination}'
        '<div class="field"><label for="owner-key">Server owner key</label>'
        '<input id="owner-key" type="password" name="key" required maxlength="256" '
        'autocomplete="off" spellcheck="false" aria-describedby="owner-key-help">'
        '<p class="field__help" id="owner-key-help">This key stays on your server.</p></div>'
        f'<button class="button button--primary" type="submit">{escape(button_label)}</button>'
        '</form>'
    )


_NAV = (
    ("overview", BASE, "Connections"),
    ("buffer", BUFFER, "Buffer"),
    ("clients", CLIENTS, "MCP clients"),
)


_STYLE = """
:root{color-scheme:light;--ink:#17243a;--muted:#546278;--line:#dae1eb;--bg:#f5f7fa;--surface:#fff;--brand:#3649aa;--brand-dark:#293985;--focus:#1469c7;--green:#0b684e;--green-bg:#e6f6ed;--amber:#845400;--amber-bg:#fff3d7;--red:#9a3030;--red-bg:#fff0ee;--blue:#22598e;--blue-bg:#eaf3ff}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,select{font:inherit}
a{color:var(--brand);text-underline-offset:3px}
a:hover{color:var(--brand-dark)}
:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
.skip-link{position:absolute;left:14px;top:-60px;z-index:50;background:#fff;padding:10px 14px;border-radius:8px}
.skip-link:focus{top:10px}
.site-header{background:#fff;border-bottom:1px solid var(--line)}
.site-header__inner{max-width:1120px;margin:auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
.brand{display:inline-flex;align-items:center;color:var(--ink);font-weight:800;font-size:18px;letter-spacing:-.025em;text-decoration:none;white-space:nowrap}
.brand__mark{position:relative;display:inline-block;width:40px;height:40px;overflow:hidden;border-radius:9px;background:#000;margin-right:9px;flex:none}
.brand__mark img{position:absolute;left:50%;top:50%;width:68px;height:68px;max-width:none;transform:translate(-50%,-50%)}
.site-nav{display:flex;flex-wrap:wrap;align-items:center;gap:5px}
.site-nav a{display:inline-flex;min-height:40px;align-items:center;padding:7px 12px;border-radius:9px;text-decoration:none;color:var(--muted);font-weight:650;font-size:14px}
.site-nav a:hover,.site-nav a[aria-current="page"]{background:#edf0fa;color:var(--brand-dark)}
.site-nav a[aria-current="page"]{box-shadow:inset 0 -2px var(--brand)}
.owner-indicator{font-size:12px;font-weight:700;color:var(--green);background:var(--green-bg);padding:6px 10px;border-radius:999px;white-space:nowrap}
.layout{max-width:1120px;margin:0 auto;padding:30px 24px 70px}
.hero{padding:6px 0 24px;max-width:760px}
.eyebrow{margin:0 0 9px;color:var(--brand);text-transform:uppercase;letter-spacing:.1em;font-size:12px;font-weight:800}
h1{font-size:clamp(30px,5vw,44px);line-height:1.12;letter-spacing:-.035em;margin:0 0 11px}
h2{font-size:20px;line-height:1.3;letter-spacing:-.018em;margin:0}
h3{font-size:16px;margin:0 0 6px}
p{margin:0 0 13px}
.hero__intro{font-size:17px;color:var(--muted);margin:0;max-width:68ch}
.stack>*+*{margin-top:18px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.grid--one{grid-template-columns:1fr}
.card{background:var(--surface);border:1px solid var(--line);border-radius:17px;box-shadow:0 3px 18px #20305708;padding:23px;min-width:0}
.card__top{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:15px}
.card__top>div{min-width:0}
.card__body{color:var(--muted)}
.card__body>*:last-child{margin-bottom:0}
.card__actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px;align-items:center}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 10px;font-size:12px;line-height:1.2;font-weight:750;white-space:nowrap;max-width:100%}
.badge:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.badge--success{color:var(--green);background:var(--green-bg)}
.badge--warning{color:var(--amber);background:var(--amber-bg)}
.badge--danger{color:var(--red);background:var(--red-bg)}
.badge--info{color:var(--blue);background:var(--blue-bg)}
.badge--neutral{color:#4b5a70;background:#edf0f4}
.alert{border:1px solid var(--line);border-left:4px solid var(--blue);background:var(--blue-bg);border-radius:11px;padding:14px 17px;margin:0 0 20px}
.alert p{margin:3px 0 0;color:var(--ink)}
.alert--success{background:var(--green-bg);border-left-color:var(--green)}
.alert--warning{background:var(--amber-bg);border-left-color:var(--amber)}
.alert--danger{background:var(--red-bg);border-left-color:var(--red)}
.button,button{display:inline-flex;justify-content:center;align-items:center;gap:8px;min-height:43px;padding:9px 16px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink);font-weight:700;text-decoration:none;cursor:pointer;line-height:1.3}
.button:hover,button:hover{background:#f2f5fc;color:var(--ink)}
.button--primary,button.button--primary{background:var(--brand);border-color:var(--brand);color:#fff}
.button--primary:hover,button.button--primary:hover{background:var(--brand-dark);color:#fff}
.button--quiet{border-color:transparent;background:transparent;color:var(--brand)}
.button--danger{color:var(--red);border-color:#f0cbc7}
.field{margin:0}
.field label{display:block;font-weight:700;color:var(--ink);margin:0 0 7px}
.field input:not([type="checkbox"]),.field select,input:not([type="checkbox"]),select{display:block;width:100%;max-width:100%;min-height:44px;background:#fff;border:1px solid #aab6c8;border-radius:9px;padding:9px 12px;color:var(--ink)}
.field__help,.hint{font-size:14px;color:var(--muted);margin:7px 0 0}
details{border-top:1px solid var(--line);padding-top:14px;margin-top:18px}
details summary{min-height:44px;padding:9px 11px;border-radius:9px;cursor:pointer;color:var(--brand);font-weight:700;list-style-position:inside}
details summary:hover{background:#edf0fa;color:var(--brand-dark)}
details[open] summary{margin-bottom:14px}
.checkbox{display:flex;align-items:flex-start;gap:10px;color:var(--ink)}
.checkbox input{margin:4px 0 0;width:18px;height:18px;flex:none;accent-color:var(--brand)}
.checkbox label{font-weight:650}
.account-list{list-style:none;padding:0;margin:0;display:grid;gap:11px}
.account-list li{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border:1px solid var(--line);border-radius:11px;color:var(--ink);min-width:0}
.account-name{font-weight:700;min-width:0;overflow-wrap:anywhere}
.muted{color:var(--muted)}
.small{font-size:14px}
.section-heading{margin:28px 0 14px}
.section-heading p{color:var(--muted);margin:5px 0 0}
.steps{padding-left:22px;margin:0;color:var(--ink)}
.steps li{padding-left:4px;margin:0 0 12px}
.steps li:last-child{margin-bottom:0}
.site-footer{max-width:1120px;margin:0 auto;padding:20px 24px 34px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
@media(max-width:720px){.site-header__inner{padding:13px 17px;gap:12px}.site-nav{order:3;width:100%;overflow-x:auto;flex-wrap:nowrap}.site-nav a{white-space:nowrap}.layout{padding:22px 17px 48px}.grid{grid-template-columns:1fr}.card{padding:19px}.card__top{flex-wrap:wrap}.card__actions .button,.card__actions button{width:100%}.site-footer{padding:16px 17px 26px}.account-list li{align-items:flex-start;flex-direction:column}}
@media(max-width:420px){.owner-indicator{display:none}.brand{font-size:17px}.hero__intro{font-size:16px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def page(
    title: object,
    intro: object,
    content: str,
    *,
    active: str = "overview",
    signed_in: bool = False,
    notice_html: str = "",
) -> str:
    """Wrap trusted content fragments in the shared connection-page shell.

    ``title`` and ``intro`` are always escaped. ``content`` and ``notice_html``
    must be HTML returned by these helpers or constructed from escaped values.
    """
    if active not in {item[0] for item in _NAV}:
        active = "overview"
    nav = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if key == active else "")
        + f'>{escape(label)}</a>'
        for key, url, label in _NAV
    )
    owner = '<span class="owner-indicator">Owner session</span>' if signed_in else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)} · X MCP</title>'
        f'<link rel="icon" type="image/jpeg" href="{LOGO}">'
        f'<style>{_STYLE}</style></head><body>'
        '<a class="skip-link" href="#main">Skip to content</a>'
        '<header class="site-header"><div class="site-header__inner">'
        f'<a class="brand" href="{BASE}" aria-label="X MCP connections home">'
        f'<span class="brand__mark" aria-hidden="true"><img src="{LOGO}" alt="" width="68" height="68"></span>X MCP</a>'
        f'<nav class="site-nav" aria-label="Connection settings">{nav}</nav>{owner}'
        '</div></header>'
        '<main class="layout" id="main"><div class="hero">'
        '<p class="eyebrow">Account setup</p>'
        f'<h1>{escape(title)}</h1><p class="hero__intro">{escape(intro)}</p>'
        f'</div>{notice_html}{content}</main>'
        '<footer class="site-footer">Your connections are managed on your own server.</footer>'
        '</body></html>'
    )
