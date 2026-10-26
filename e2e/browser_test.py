"""End-to-end browser test of FairFlow with Chrome's virtual WebAuthn authenticator.

Run against a FRESH database (it creates shops/users):
    pip install playwright && playwright install chromium
    ADMIN_USERNAME=admin python setup_db.py          # note the activation code it prints
    uvicorn main:app --port 8000                       # in another terminal
    python e2e/browser_test.py /tmp/shots <ADMIN-CODE>


Each person gets their own browser context + virtual platform authenticator
(internal transport, user verification = fingerprint), like separate phones/laptops.
"""
import os
import re
import sys
from datetime import date

from playwright.sync_api import expect, sync_playwright

BASE = "http://localhost:8000"
SHOTS = sys.argv[1]
ADMIN_CODE = sys.argv[2]
CHROME = os.getenv("CHROME_PATH")  # optional: use a specific Chromium binary
problems = []


def device(browser, viewport):
    ctx = browser.new_context(viewport=viewport)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and "ERR_TUNNEL" not in m.text and "status of 400" not in m.text and "status of 401" not in m.text and problems.append(f"console: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    auth = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})
    page.cdp, page.auth_id = cdp, auth["authenticatorId"]
    return page


def toast(page, text):
    expect(page.locator(".toast").filter(has_text=text).first).to_be_visible(timeout=10000)
    page.evaluate("document.querySelector('.toasts').replaceChildren()")  # so the next check sees only new toasts
    expect(page.locator("main [aria-busy]")).to_have_count(0, timeout=15000)  # page refresh finished


def shot(page, name, full=True):
    page.screenshot(path=f"{SHOTS}/{name}.png", full_page=full)


def nav(page, label):
    if page.locator(".menu-toggle").is_visible():
        page.locator(".menu-toggle").click()
    link = page.locator("nav").get_by_role("link", name=label, exact=True)
    href = link.get_attribute("href")
    link.click()
    expect(page).to_have_url(re.compile(re.escape(href) + "$"))
    expect(page.locator("main [aria-busy]")).to_have_count(0, timeout=15000)
    expect(page.locator("main h1")).to_be_visible()


def activate(page, username, code):
    page.goto(BASE)
    page.get_by_role("tab", name="Activate account").click()
    page.get_by_label("Username").fill(username)
    page.get_by_label("Activation code").fill(code)
    page.get_by_role("button", name="Register my fingerprint").click()


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()

    # ---------- Admin: activate with the setup_db.py code ----------
    admin = device(browser, {"width": 1366, "height": 860})
    admin.goto(BASE)
    expect(admin.locator(".support-note")).to_have_text(re.compile("Fingerprint sensor ready"))
    shot(admin, "01-sign-in", full=False)
    activate(admin, "admin", "wrong-code1")
    toast(admin, "Invalid or expired activation code")
    admin.get_by_label("Activation code").fill(ADMIN_CODE)
    admin.get_by_role("button", name="Register my fingerprint").click()
    expect(admin.get_by_role("heading", name="Overview")).to_be_visible()
    toast(admin, "Your account is active")

    # Shops, villages, commodities
    nav(admin, "Shops & villages")
    admin.get_by_label("Shop name").fill("FPS Guindy")
    admin.get_by_label("Location").fill("Guindy, Chennai")
    admin.get_by_role("button", name="Add shop").click()
    toast(admin, "Added FPS Guindy")
    for v in ("Kotturpuram", "Saidapet"):
        admin.get_by_label("Village name").fill(v)
        admin.get_by_label("Served by").select_option(label="FPS Guindy")
        admin.get_by_role("button", name="Add village").click()
        toast(admin, f"Added {v}")
    nav(admin, "Commodities")
    for c in ("Rice", "Wheat", "Sugar"):
        admin.get_by_label("Name", exact=True).fill(c)
        admin.get_by_role("button", name="Add commodity").click()
        toast(admin, f"Added {c}")
    expect(admin.locator("tbody tr")).to_have_count(3)

    nav(admin, "Shops & villages")
    for village, qty in (("Kotturpuram", ("50", "20", "10")), ("Saidapet", ("40", "15", "8"))):
        admin.locator("tr", has_text=village).get_by_role("button", name="Quantities").click()
        dialog = admin.locator("dialog")
        for name, value in zip(("Rice", "Wheat", "Sugar"), qty):
            dialog.get_by_label(name).fill(value)
        dialog.get_by_role("button", name="Save quantities").click()
        toast(admin, f"Saved 3 changes for {village}")
    shot(admin, "02-admin-places")

    # Staff account -> one-time activation code
    nav(admin, "People & access")
    admin.get_by_label("Username").fill("officer1")
    admin.get_by_label("Role").select_option("officer")
    admin.get_by_label("Shop").select_option(label="FPS Guindy")
    admin.get_by_role("button", name="Create account").click()
    officer_code = admin.locator(".code-reveal").inner_text().strip()
    assert re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", officer_code), officer_code
    shot(admin, "03-activation-code", full=False)
    admin.get_by_role("button", name="Done").click()

    # Sign out and sign back in with the fingerprint only
    admin.get_by_role("button", name="Sign out").click()
    toast(admin, "Signed out")
    admin.locator(".sensor").click()
    expect(admin.get_by_role("heading", name="Overview")).to_be_visible()
    toast(admin, "Signed in as admin")

    # ---------- Beneficiary: self-signup on a phone ----------
    ben = device(browser, {"width": 390, "height": 844})
    ben.goto(BASE)
    ben.get_by_role("tab", name="New beneficiary").click()
    ben.get_by_label("Username").fill("meena")
    ben.get_by_label("Ration shop").select_option(label="FPS Guindy, Guindy, Chennai")
    ben.get_by_label("Mobile number").fill("+919876543210")
    shot(ben, "04-phone-signup", full=False)
    ben.get_by_role("button", name="Register my fingerprint").click()
    toast(ben, "Your shop officer will approve your account")
    expect(ben.get_by_role("heading", name="My ration shop")).to_be_visible()
    expect(ben.locator("main")).to_contain_text("still needs to approve your account")  # nothing shown yet

    # ---------- Officer: activate on own laptop, receive stock, distribute ----------
    officer = device(browser, {"width": 1366, "height": 860})
    activate(officer, "officer1", officer_code.lower())
    expect(officer.get_by_role("heading", name="Today")).to_be_visible()
    expect(officer.locator(".callout .big")).to_have_text("None ready")

    # Officer approves the new beneficiary; her open session sees the shop at once.
    nav(officer, "Beneficiaries")
    expect(officer.locator(".banner", has_text="waiting for your approval")).to_be_visible()
    officer.locator("tr", has_text="meena").get_by_role("button", name="Approve").click()
    toast(officer, "meena approved")
    nav(ben, "My ration shop")
    expect(ben.get_by_role("heading", name="FPS Guindy")).to_be_visible()

    nav(officer, "Stock")
    for name, qty in (("Rice", "120"), ("Wheat", "40"), ("Sugar", "30")):
        officer.get_by_label("Commodity").select_option(label=name)
        officer.get_by_label("Quantity received").fill(qty)
        officer.get_by_role("button", name="Add to stock").click()
        toast(officer, "Stock added")
    officer.locator("tr", has_text="Sugar").get_by_role("button", name="Availability").click()
    officer.locator("dialog").get_by_label("Status").select_option("seasonally_suspended")
    officer.locator("dialog").get_by_role("button", name="Save status").click()
    toast(officer, "Reason is required when status is not active")  # server-side rule surfaces
    officer.locator("dialog").get_by_label("Reason").fill("Not supplied this month")
    officer.locator("dialog").get_by_role("button", name="Save status").click()
    toast(officer, "Sugar status saved")
    shot(officer, "05-officer-stock")

    nav(officer, "Today")
    expect(officer.locator(".callout .big")).to_have_text("None ready")  # sugar suspended
    nav(officer, "Stock")
    officer.locator("tr", has_text="Sugar").get_by_role("button", name="Availability").click()
    officer.locator("dialog").get_by_label("Status").select_option("active")
    officer.locator("dialog").get_by_role("button", name="Save status").click()
    toast(officer, "Sugar status saved")

    nav(officer, "Today")
    expect(officer.locator(".callout .big")).to_have_text("Kotturpuram")
    shot(officer, "06-officer-today")
    officer.get_by_role("button", name="Review and distribute").click()
    expect(officer.locator("dialog h2")).to_have_text("Kotturpuram: ready to distribute")
    shot(officer, "07-distribute-dialog", full=False)
    officer.get_by_role("button", name="Distribute to Kotturpuram").click()
    toast(officer, "Distributed to Kotturpuram. Stock updated.")
    expect(officer.locator(".callout .big")).to_have_text("Saidapet")

    nav(officer, "Villages")
    expect(officer.locator("tr", has_text="Kotturpuram")).to_contain_text(date.today().strftime("%Y")[0:4])

    # Past history -> forecast chart
    nav(officer, "Demand forecast")
    def month_15(n_back):
        t = date.today()
        y, m = divmod(t.year * 12 + t.month - 1 - n_back, 12)
        return date(y, m + 1, 15).isoformat()
    for d, q in ((month_15(4), "300"), (month_15(3), "320"), (month_15(2), "360"), (month_15(1), "380")):
        officer.get_by_label("Commodity").select_option(label="Rice")
        officer.get_by_label("Quantity").fill(q)
        officer.get_by_label("Date").fill(d)
        officer.get_by_role("button", name="Add record").click()
        toast(officer, "Record added")
    expect(officer.locator("svg.chart")).to_be_visible()
    expect(officer.locator(".stat .name").first).to_contain_text("forecast for")
    shot(officer, "08-forecast")

    # ---------- Beneficiary: SMS log + complaint ----------
    nav(ben, "Messages")
    expect(ben.locator(".timeline li").first).to_contain_text("Ration distribution for Kotturpuram")
    expect(ben.locator(".timeline li").first).to_contain_text("ரேஷன்")
    nav(ben, "Complaints")
    ben.get_by_label("What is it about?").select_option("quantity")
    ben.get_by_label("Describe the problem").fill("Received 4 kg rice instead of 5 kg")
    ben.get_by_role("button", name="Send complaint").click()
    toast(ben, "Complaint sent")
    nav(ben, "My ration shop")
    shot(ben, "09-phone-home")

    # ---------- Officer resolves; beneficiary sees status + SMS ----------
    nav(officer, "Complaints")
    officer.locator(".timeline li", has_text="4 kg rice").get_by_role("button", name="Update").click()
    officer.locator("dialog").get_by_label("New status").select_option("resolved")
    officer.locator("dialog").get_by_role("button", name="Update complaint").click()
    toast(officer, "resolution note is required")
    officer.locator("dialog").get_by_label("Note to the beneficiary").fill("Shortfall of 1 kg given on 16 Oct")
    officer.locator("dialog").get_by_role("button", name="Update complaint").click()
    toast(officer, "updated")
    nav(ben, "Complaints")
    expect(ben.locator(".timeline li").first).to_contain_text("Resolved")
    nav(ben, "Messages")
    expect(ben.locator(".timeline li").first).to_contain_text("is now resolved")

    # Beneficiary cannot open staff pages (role-based navigation + API checks)
    ben.goto(f"{BASE}/#/people")
    expect(ben.get_by_role("heading", name="FPS Guindy")).to_be_visible()

    # Second phone: code WITHOUT switching off the first phone -> both work
    nav(officer, "Beneficiaries")
    expect(officer.locator(".banner", has_text="Simulation only")).to_be_visible()
    officer.locator("tr", has_text="meena").get_by_role("button", name="New activation code").click()
    officer.locator("dialog").get_by_label(re.compile("lost or stolen")).uncheck()
    officer.locator("dialog").get_by_role("button", name="Create code").click()
    recovery = officer.locator(".code-reveal").inner_text().strip()
    officer.get_by_role("button", name="Done").click()
    new_phone = device(browser, {"width": 390, "height": 844})
    activate(new_phone, "meena", recovery)
    expect(new_phone.get_by_role("heading", name="FPS Guindy")).to_be_visible()
    nav(new_phone, "My passkeys")
    expect(new_phone.locator("tbody tr")).to_have_count(2)

    # Lost phones: officer switches off all of meena's devices -> sessions end, fingerprint refused
    officer.reload()
    expect(officer.locator("main [aria-busy]")).to_have_count(0, timeout=15000)
    officer.locator("tr", has_text="meena").get_by_role("button", name="Switch off devices").click()
    officer.locator("dialog").get_by_role("button", name="Switch off devices").click()
    toast(officer, "Devices switched off")
    ben.reload()
    expect(ben.locator(".auth-panel")).to_be_visible()  # signed out
    ben.locator(".sensor").click()
    expect(ben.locator(".toast").filter(has_text="not registered")).to_be_visible(timeout=15000)
    shot(ben, "09b-phone-switched-off", full=False)

    # ---------- Admin views ----------
    nav(admin, "Overview")
    shot(admin, "10-admin-overview")
    nav(admin, "People & access")
    expect(admin.locator("tr", has_text="officer1")).to_contain_text("Fingerprint on 1 device")
    shot(admin, "10b-admin-people")
    nav(admin, "Activity log")
    expect(admin.locator("tbody")).to_contain_text("Signed in with fingerprint")
    expect(admin.locator("tbody")).to_contain_text("Village served")
    nav(admin, "My passkeys")
    shot(admin, "11-passkeys")

    # Unregistered device cannot sign in
    stranger = device(browser, {"width": 1366, "height": 860})
    stranger.goto(BASE)
    stranger.locator(".sensor").click()
    expect(stranger.locator(".toast")).to_be_visible(timeout=15000)
    print("stranger toast:", stranger.locator(".toast").first.inner_text())
    stranger.screenshot(path=f"{SHOTS}/12-stranger.png")

    browser.close()

print("PROBLEMS:", problems or "none")
print("E2E PASSED" if not problems else "E2E FINISHED WITH PROBLEMS")
