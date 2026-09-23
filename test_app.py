import os
import tempfile
from fastapi.testclient import TestClient

# Use temporary test database
temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = temp_db.name

from main import app, init_db

init_db()
client = TestClient(app)

def run_tests():
    print("=== STARTING COMPREHENSIVE TESTS ===")

    # 1. Check email for unregistered user
    res = client.post("/api/auth/check-email", json={"email": "hai@vib.com"})
    assert res.status_code == 200, res.text
    assert res.json()["exists"] is False
    print("✓ Check email unregistered passed")

    # 2. Request OTP for registration
    res = client.post("/api/auth/request-otp", json={"email": "hai@vib.com", "purpose": "REGISTER"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert "dev_code" in data
    otp_code = data["dev_code"]
    print(f"✓ Request OTP passed (code: {otp_code})")

    # 3. Register with OTP & set PIN 4 digits
    res = client.post("/api/auth/register-or-reset", json={
        "email": "hai@vib.com",
        "code": otp_code,
        "purpose": "REGISTER",
        "pin": "1234",
        "display_name": "Thành Thanh Hải"
    })
    assert res.status_code == 200, res.text
    hai_data = res.json()
    hai_id = hai_data["user"]["id"]
    hai_token = hai_data["token"]
    print("✓ Register and set PIN 4 digits passed")

    # 4. Check email now exists
    res = client.post("/api/auth/check-email", json={"email": "hai@vib.com"})
    assert res.json()["exists"] is True
    assert res.json()["display_name"] == "Thành Thanh Hải"
    print("✓ Check email registered passed")

    # 5. Login with PIN
    res = client.post("/api/auth/login", json={"email": "hai@vib.com", "pin": "9999"})
    assert res.status_code == 400
    res = client.post("/api/auth/login", json={"email": "hai@vib.com", "pin": "1234"})
    assert res.status_code == 200
    assert res.json()["user"]["id"] == hai_id
    print("✓ Login with correct/incorrect PIN passed")

    # 6. Reset PIN flow
    res = client.post("/api/auth/request-otp", json={"email": "hai@vib.com", "purpose": "RESET_PIN"})
    assert res.status_code == 200
    reset_otp = res.json()["dev_code"]
    res = client.post("/api/auth/register-or-reset", json={
        "email": "hai@vib.com",
        "code": reset_otp,
        "purpose": "RESET_PIN",
        "pin": "5678"
    })
    assert res.status_code == 200
    # Old PIN 1234 should fail, new PIN 5678 should succeed
    res = client.post("/api/auth/login", json={"email": "hai@vib.com", "pin": "1234"})
    assert res.status_code == 400
    res = client.post("/api/auth/login", json={"email": "hai@vib.com", "pin": "5678"})
    assert res.status_code == 200
    hai_token = res.json()["token"]
    hai_headers = {"Authorization": f"Bearer {hai_token}"}
    print("✓ Reset PIN flow passed")

    # 7. Update profile (bank info)
    res = client.patch("/api/users/me", json={
        "bank_name": "VIB",
        "bank_account_number": "000123456789",
        "bank_account_name": "THANH THANH HAI"
    }, headers=hai_headers)
    assert res.status_code == 200
    assert res.json()["user"]["bank_name"] == "VIB"
    print("✓ Update profile with bank info passed")

    # 8. Register user Minh and user Lan
    def register_user(email, name, pin="1111"):
        r = client.post("/api/auth/request-otp", json={"email": email, "purpose": "REGISTER"})
        code = r.json()["dev_code"]
        r = client.post("/api/auth/register-or-reset", json={
            "email": email, "code": code, "purpose": "REGISTER", "pin": pin, "display_name": name
        })
        return r.json()["user"]["id"], r.json()["token"]

    minh_id, minh_token = register_user("minh@vib.com", "Nguyễn Văn Minh")
    lan_id, lan_token = register_user("lan@vib.com", "Trần Thị Lan")
    minh_headers = {"Authorization": f"Bearer {minh_token}"}
    lan_headers = {"Authorization": f"Bearer {lan_token}"}
    print("✓ Registered multiple team members passed")

    # 9. Record Fund Contribution: Hai đóng 1,000,000 đ, Minh đóng 500,000 đ
    res = client.post("/api/fund/contributions", json={
        "amount": 1000000,
        "transaction_date": "2026-09-23",
        "note": "Hải đóng quỹ tháng 9"
    }, headers=hai_headers)
    assert res.status_code == 200

    res = client.post("/api/fund/contributions", json={
        "amount": 500000,
        "transaction_date": "2026-09-23",
        "note": "Minh đóng quỹ tháng 9"
    }, headers=minh_headers)
    assert res.status_code == 200

    # Check fund summary
    res = client.get("/api/fund/summary", headers=hai_headers)
    fund = res.json()
    assert fund["balance"] == 1500000
    assert fund["total_in"] == 1500000
    assert fund["total_out"] == 0
    assert len(fund["members"]) == 3
    print("✓ Group fund contributions and summary passed (Balance: 1,500,000 đ)")

    # 10. Create PERSONAL bill: Hải chi trả ăn trưa 300,000 đ chia đều cho [Hải, Minh, Lan]
    res = client.post("/api/bills", json={
        "title": "Ăn trưa bún chả",
        "description": "Bún chả phố cổ",
        "category": "Ăn uống",
        "total_amount": 300000,
        "expense_date": "2026-09-23",
        "payer_user_id": hai_id,
        "source_type": "PERSONAL",
        "split_mode": "EQUAL",
        "participants": [
            {"user_id": hai_id},
            {"user_id": minh_id},
            {"user_id": lan_id}
        ]
    }, headers=hai_headers)
    assert res.status_code == 200
    bill1_id = res.json()["bill_id"]
    print(f"✓ Created PERSONAL bill EQUAL split (Bill #{bill1_id})")

    # Verify Debt calculation:
    # Minh owes Hai 100,000 đ. Lan owes Hai 100,000 đ.
    res = client.get("/api/debts", headers=minh_headers)
    minh_debts = res.json()
    assert len(minh_debts["i_owe"]) == 1
    assert minh_debts["i_owe"][0]["share_amount"] == 100000
    assert minh_debts["i_owe"][0]["creditor_id"] == hai_id
    assert minh_debts["i_owe"][0]["payment_status"] == "UNPAID"

    res = client.get("/api/debts", headers=hai_headers)
    hai_debts = res.json()
    assert len(hai_debts["others_owe"]) == 2
    assert sum(d["share_amount"] for d in hai_debts["others_owe"]) == 200000
    print("✓ Debts generated accurately: Minh & Lan each owe 100,000 đ to Hải")

    # 11. Payment Flow:
    # Minh reports payment ("Tôi đã trả")
    res = client.post(f"/api/bills/{bill1_id}/payment-report", headers=minh_headers)
    assert res.status_code == 200
    # Check Minh debt status is PAYMENT_REPORTED
    res = client.get("/api/debts", headers=minh_headers)
    assert res.json()["i_owe"][0]["payment_status"] == "PAYMENT_REPORTED"
    # Hai receives notification
    res = client.get("/api/notifications", headers=hai_headers)
    assert res.json()["unread_count"] >= 1
    print("✓ Debtor reported payment ('Tôi đã trả') and notification sent to payer")

    # Hai confirms payment ("Đã nhận tiền")
    res = client.post(f"/api/bills/{bill1_id}/payment-confirm", json={"user_id": minh_id}, headers=hai_headers)
    assert res.status_code == 200
    # Minh should no longer owe this bill
    res = client.get("/api/debts", headers=minh_headers)
    assert len(res.json()["i_owe"]) == 0
    # Lan still owes
    res = client.get("/api/debts", headers=lan_headers)
    assert len(res.json()["i_owe"]) == 1
    print("✓ Payer confirmed payment ('Đã nhận tiền'), debt cleared for debtor")

    # 12. Create GROUP_FUND bill: Mua cafe 150,000 đ dùng quỹ nhóm
    res = client.post("/api/bills", json={
        "title": "Cafe Highlands",
        "description": "Team building cafe",
        "category": "Cafe",
        "total_amount": 150000,
        "expense_date": "2026-09-23",
        "payer_user_id": hai_id,
        "source_type": "GROUP_FUND",
        "split_mode": "NONE",
        "participants": [
            {"user_id": hai_id},
            {"user_id": minh_id}
        ]
    }, headers=hai_headers)
    assert res.status_code == 200
    bill2_id = res.json()["bill_id"]

    # Verify no debts created for GROUP_FUND bill
    res = client.get("/api/debts", headers=minh_headers)
    assert len(res.json()["i_owe"]) == 0 # Still 0, no debt from group fund!

    # Verify Fund balance decreased: 1,500,000 - 150,000 = 1,350,000 đ
    res = client.get("/api/fund/summary", headers=hai_headers)
    fund = res.json()
    assert fund["total_out"] == 150000
    assert fund["balance"] == 1350000
    print("✓ GROUP_FUND bill created, no debt created, fund balance: 1,350,000 đ")

    # 13. Create PERSONAL bill with CUSTOM split: Tổng 250k: Lan 150k, Minh 100k
    res = client.post("/api/bills", json={
        "title": "Taxi đi họp",
        "category": "Di chuyển",
        "total_amount": 250000,
        "expense_date": "2026-09-23",
        "payer_user_id": hai_id,
        "source_type": "PERSONAL",
        "split_mode": "CUSTOM",
        "participants": [
            {"user_id": lan_id, "share_amount": 150000},
            {"user_id": minh_id, "share_amount": 100000}
        ]
    }, headers=hai_headers)
    assert res.status_code == 200
    bill3_id = res.json()["bill_id"]
    print("✓ PERSONAL bill with CUSTOM split created successfully")

    # 14. Edit bill: update bill3 total to 300,000 đ (Lan 200k, Minh 100k)
    res = client.patch(f"/api/bills/{bill3_id}", json={
        "title": "Taxi đi họp VIP",
        "category": "Di chuyển",
        "total_amount": 300000,
        "expense_date": "2026-09-23",
        "payer_user_id": hai_id,
        "source_type": "PERSONAL",
        "split_mode": "CUSTOM",
        "participants": [
            {"user_id": lan_id, "share_amount": 200000},
            {"user_id": minh_id, "share_amount": 100000}
        ]
    }, headers=hai_headers)
    assert res.status_code == 200
    # Check Lan debt updated to 200k
    res = client.get("/api/debts", headers=lan_headers)
    lan_debts = [d for d in res.json()["i_owe"] if d["bill_id"] == bill3_id]
    assert len(lan_debts) == 1
    assert lan_debts[0]["share_amount"] == 200000
    print("✓ Edit bill recalculated split and updated debt correctly")

    # 15. Delete bill: delete bill3
    res = client.delete(f"/api/bills/{bill3_id}", headers=hai_headers)
    assert res.status_code == 200
    # Verify bill3 is deleted and not returned in bills list or debts
    res = client.get("/api/debts", headers=lan_headers)
    lan_debts = [d for d in res.json()["i_owe"] if d["bill_id"] == bill3_id]
    assert len(lan_debts) == 0
    print("✓ Delete bill soft-deleted bill and cleared associated debts")

    # 16. Dashboard verification
    res = client.get("/api/dashboard", headers=hai_headers)
    dash = res.json()
    assert dash["fund_balance"] == 1350000
    assert dash["total_i_owe"] == 0
    assert dash["total_others_owe"] == 100000 # Only Lan owes 100k for bill1
    assert len(dash["recent_bills"]) >= 2
    assert len(dash["recent_fund_transactions"]) >= 3
    print("✓ Dashboard metrics verified perfectly")

    # 17. Notifications read
    res = client.get("/api/notifications", headers=hai_headers)
    notifs = res.json()["notifications"]
    assert len(notifs) > 0
    first_notif_id = notifs[0]["id"]
    res = client.post(f"/api/notifications/{first_notif_id}/read", headers=hai_headers)
    assert res.status_code == 200
    res = client.post("/api/notifications/read-all", headers=hai_headers)
    assert res.status_code == 200
    res = client.get("/api/notifications", headers=hai_headers)
    assert res.json()["unread_count"] == 0
    print("✓ Notifications mark-read and read-all verified")

    print("\n🎉 ALL 17 INTEGRATION & BUSINESS RULES PASSED PERFECTLY!")

if __name__ == "__main__":
    run_tests()
