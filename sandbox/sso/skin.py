"""Presentation only: a stylesheet for the stock Django admin, whose own static files aren't served here.

It may change only colours, fonts, spacing and borders inside <main>, plus the outer layout (header, sidebar).
It doesn't change what the agent reads: <main>'s text, the interactive elements, their labels and their
visibility stay identical. tests/test_sso_skin.py checks that on every admin page the agent uses.
"""
CSS = """
html,body{background:#f2f4f8}
body{margin:0;font:14px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;color:#1f2937}
a{color:#2457c5;text-decoration:none}
a:hover{text-decoration:underline}
.skip-to-content-link{display:block;font-size:11px;color:#94a3b8;padding:2px 28px;background:#0f1d3d}
.base-svgs{position:absolute;width:0;height:0}
#header{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:14px 28px;
        background:linear-gradient(90deg,#0f1d3d,#1d3f8f);color:#c7d4ee}
#site-name{font-size:22px;font-weight:700;letter-spacing:.2px}
#site-name a{color:#fff}
#site-name a::before{content:"\\25C6";color:#6ea8ff;margin-right:10px;font-size:18px}
#user-tools{font-size:12.5px;text-align:right;color:#c7d4ee}
#user-tools a{color:#fff}
#user-tools strong{color:#fff}
#logout-form{margin:2px 0 0}
#logout-form button{background:transparent;border:1px solid rgba(255,255,255,.35);color:#fff;border-radius:6px;padding:2px 10px;font:inherit;cursor:pointer}
.theme-toggle{background:transparent;border:0;color:#c7d4ee;padding:2px 4px;margin-left:6px}
.theme-toggle .visually-hidden{font-size:0}
.theme-toggle svg{display:none}
nav[aria-label="Breadcrumbs"]{background:#fff;border-bottom:1px solid #e3e8f0}
.breadcrumbs{display:flex;gap:8px;list-style:none;margin:0;padding:10px 28px;font-size:13px;color:#64748b}
.breadcrumbs li+li::before{content:"\\203A";margin-right:8px;color:#94a3b8}
#main{display:flex;align-items:stretch;min-height:calc(100vh - 110px)}
#toggle-nav-sidebar{width:10px;border:0;background:#e8edf5;cursor:pointer}
#nav-sidebar{width:230px;flex:none;background:#fff;border-right:1px solid #e3e8f0;padding:16px 14px}
#nav-filter{width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #d5dce8;border-radius:7px;font:inherit;margin-bottom:14px}
#nav-sidebar table{width:100%;border-collapse:collapse}
#nav-sidebar caption{text-align:left;padding:6px 8px;font-size:11.5px;font-weight:700;letter-spacing:.06em;color:#64748b;background:#f2f5fa;border-radius:6px}
#nav-sidebar caption a{color:#64748b}
#nav-sidebar th,#nav-sidebar td{padding:7px 8px;border-bottom:1px solid #eef1f6;text-align:left;font-weight:500}
#nav-sidebar tr.current-model{background:#eaf1ff}
#nav-sidebar .visually-hidden,#content .visually-hidden{font-size:0;line-height:0;margin:0;padding:0}
#nav-sidebar .addlink{font-size:12px;color:#16803c}
main.content{flex:1;min-width:0}
#content{padding:24px 32px 40px;max-width:1100px}
#content h1{font-size:24px;font-weight:650;color:#0f172a;margin:0 0 18px}
#content h2{font-size:15px;color:#0f172a}
.visually-hidden,#content .visually-hidden{font-size:0;line-height:0;margin:0;padding:0}
.object-tools{float:right;list-style:none;margin:0;padding:0}
.object-tools a{background:#2457c5;color:#fff;padding:7px 14px;border-radius:8px;font-weight:600}
.object-tools a:hover{text-decoration:none;background:#1d4aa8}
.messagelist{list-style:none;padding:0;margin:0 0 16px}
.messagelist li{padding:10px 14px;border-radius:8px;background:#e7f6ec;color:#14632f;border:1px solid #b9e3c6;margin-bottom:8px}
.messagelist li.warning{background:#fff6e0;color:#7a5200;border-color:#f0d58c}
.messagelist li.error{background:#fdecec;color:#991b1b;border-color:#f3c0c0}
.errornote,.errorlist{color:#b42318}
.errornote{background:#fdecec;border:1px solid #f3c0c0;padding:10px 14px;border-radius:8px}
.errorlist{list-style:none;padding:0;margin:4px 0}
.changelist-form-container{display:flex;flex-direction:row-reverse;align-items:flex-start;gap:24px}
.changelist-form-container>div{flex:1;min-width:0}
#changelist-filter{flex:none;width:220px;background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:12px 16px;font-size:13px}
#changelist-filter h2{margin:0 0 6px;font-size:12px;letter-spacing:.06em;color:#64748b}
#changelist-filter h3{margin:0 0 6px;font-size:12px;font-weight:500}
#changelist-filter summary{font-weight:600;color:#334155;margin-top:8px;cursor:pointer}
#changelist-filter ul{list-style:none;margin:4px 0 0;padding:0 0 0 10px}
#changelist-filter li.selected a{color:#0f172a;font-weight:700}
#toolbar{background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:10px 14px;margin-bottom:12px}
#changelist-search img{width:15px;height:15px;opacity:.55;vertical-align:middle;margin-right:6px}
#searchbar{padding:7px 10px;border:1px solid #cfd7e4;border-radius:7px;font:inherit;width:300px}
.actions{background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:8px 14px;margin-bottom:10px;font-size:13px}
.actions select{padding:5px 8px;border:1px solid #cfd7e4;border-radius:6px;font:inherit}
.actions .button{padding:5px 12px;border:1px solid #cfd7e4;border-radius:6px;background:#f7f9fc;font:inherit;cursor:pointer}
#result_list{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8f0;border-radius:10px;overflow:hidden}
#result_list thead th{background:#f5f7fb;color:#475569;font-size:12px;font-weight:600;text-align:left;padding:9px 12px;border-bottom:1px solid #e3e8f0}
#result_list td,#result_list tbody th{padding:9px 12px;border-bottom:1px solid #eef1f6;text-align:left;font-weight:400}
#result_list tbody tr:hover{background:#f7faff}
#result_list tbody th a{font-weight:600}
p.paginator{color:#64748b;font-size:13px}
fieldset.module{background:#fff;border:1px solid #e3e8f0;border-radius:12px;padding:6px 22px 10px;margin:0 0 18px}
fieldset.module>h2,fieldset.module legend{font-size:13px;font-weight:700;color:#0f172a;padding:12px 0 6px}
.form-row{padding:12px 0;border-bottom:1px solid #f0f2f6}
.form-row:last-child{border-bottom:0}
.form-row label{font-weight:600;color:#1f2937;margin-right:10px}
.form-row label.required::after{content:" *";color:#d14343}
.form-row fieldset{border:0;padding:0;margin:0}
.form-row legend{font-weight:600;color:#1f2937;padding:0}
.help,.form-row .help,p.help{color:#64748b;font-size:12px;margin:3px 0 6px}
input[type=text],input[type=password],input[type=email],input[type=number],input.vTextField,input.vDateField,input.vTimeField,textarea{
  padding:8px 11px;border:1px solid #cfd7e4;border-radius:8px;font:inherit;background:#fff;color:#0f172a;min-width:320px}
input[type=text]:focus,input[type=password]:focus,input[type=email]:focus,textarea:focus{outline:2px solid #9cc0ff;border-color:#2457c5}
input.vDateField,input.vTimeField{min-width:140px}
select{padding:6px 8px;border:1px solid #cfd7e4;border-radius:8px;font:inherit;background:#fff}
select[multiple]{min-width:320px}
input[type=checkbox],input[type=radio]{accent-color:#2457c5;width:16px;height:16px;vertical-align:-3px}
.submit-row{background:#fff;border:1px solid #e3e8f0;border-radius:12px;padding:14px 18px;margin:8px 0 0}
.submit-row input[type=submit],#changelist-search input[type=submit],.login .submit-row input{
  background:#eef2f8;color:#1f2937;border:1px solid #cfd7e4;border-radius:8px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer;margin:2px 6px 2px 0}
.submit-row input.default,.login .submit-row input{background:#2457c5;border-color:#2457c5;color:#fff}
.submit-row a.deletelink{color:#b42318;margin-left:10px}
body.login #container{max-width:440px;margin:70px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 18px 50px rgba(15,29,61,.18)}
body.login #main{min-height:0}
body.login .submit-row{border:0;padding:8px 0 0}
body.login #content{padding:26px 30px 30px}
#footer{padding:0}
"""

TAG = ("<style id=\"acme-skin\">" + CSS + "</style>").encode()


class Skin:
    """Django middleware: inserts the stylesheet into HTML pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html") and not response.streaming:
            response.content = response.content.replace(b"</head>", TAG + b"</head>", 1)
        return response
