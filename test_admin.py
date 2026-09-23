import os
import tempfile
import sqlite3
from fastapi.testclient import TestClient

# Use temporary test database
temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = temp_db.name

from main import app, init_db, hash_pin

init_db()
client = TestClient(app)

def test_admin_flow():
    print("=== STARTING ADMIN USER MANAGEMENT TESTS ===")

    # Setup Admin user and Normal user
    conn = sqlite3.connect(temp_db.name)
    now = "2026-09-23T10:00:00Z"
    h1 = hash_pin("8888")
    h2 = hash_pin("1234")
    with conn:
        conn.execute("INSERT INTO users (email, pin_hash, display_name, role, status, created_at, updated_at) VALUES (?, ?, ?, 'admin', 'active', ?, ?)",
                     ("admin@vib.com", h1, "Admin Hai", now, now))
        conn.execute("INSERT INTO users (email, pin_hash, display_name, role, status, created_at, updated_at) VALUES (?, ?, ?, 'user', 'active', ?, ?)",
                     ("member@vib.com", h2, "Member Nam", now, now))
    conn.close()

    # Login Admin
    res = client.post("/api/auth/login", json={"email": "admin@vib.com", "pin": "8888"})
    assert res.status_code == 200, res.text
    admin_token = res.json()["token"]
    admin_id = res.json()["user"]["id"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    assert res.json()["user"]["role"] == "admin"
    print("✓ Admin login success")

    # Login Normal user
    res = client.post("/api/auth/login", json={"email": "member@vib.com", "pin": "1234"})
    assert res.status_code == 200, res.text
    user_token = res.json()["token"]
    member_id = res.json()["user"]["id"]
    user_headers = {"Authorization": f"Bearer {user_token}"}
    assert res.json()["user"]["role"] == "user"
    print("✓ Normal member login success")

    # Case 1: User thường truy cập GET /api/admin/users -> Expected 403
    res = client.get("/api/admin/users", headers=user_headers)
    assert res.status_code == 403, res.text
    print("✓ Case 1 Passed: Normal user gets 403 on admin endpoint")

    # Admin lists users
    res = client.get("/api/admin/users", headers=admin_headers)
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 2
    assert all("pin_hash" not in u for u in items)
    print("✓ Admin list users verified, pin_hash excluded")

    # Case 2: Admin đổi user thành admin -> role = admin
    res = client.patch(f"/api/admin/users/{member_id}/role", json={"role": "admin"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.get("/api/auth/me", headers=user_headers)
    assert res.status_code == 200 and res.json()["role"] == "admin"
    print("✓ Case 2 Passed: Member promoted to admin successfully")

    # Case 3: Có 2 admin. Admin A hạ Admin B thành user -> Success
    res = client.patch(f"/api/admin/users/{member_id}/role", json={"role": "user"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.get("/api/auth/me", headers=user_headers)
    assert res.status_code == 200 and res.json()["role"] == "user"
    print("✓ Case 3 Passed: Demote back to user successfully when 2 admins exist")

    # Case 4: Chỉ còn 1 active admin. Admin đó tự đổi thành user -> 409 LAST_ADMIN_PROTECTION
    res = client.patch(f"/api/admin/users/{admin_id}/role", json={"role": "user"}, headers=admin_headers)
    assert res.status_code == 409, res.text
    assert res.json().get("error") == "LAST_ADMIN_PROTECTION"
    print("✓ Case 4 Passed: Last admin demotion rejected with 409 LAST_ADMIN_PROTECTION")

    # Case 5: Chỉ còn 1 active admin. Admin đó bị disable -> 409 LAST_ADMIN_PROTECTION
    res = client.patch(f"/api/admin/users/{admin_id}/status", json={"status": "disabled"}, headers=admin_headers)
    assert res.status_code == 409, res.text
    assert res.json().get("error") == "LAST_ADMIN_PROTECTION"
    print("✓ Case 5 Passed: Last admin disable rejected with 409 LAST_ADMIN_PROTECTION")

    # Case 6: Admin disable một user đang login -> API disable thành công; request tiếp theo bị 403 ACCOUNT_DISABLED
    res = client.patch(f"/api/admin/users/{member_id}/status", json={"status": "disabled"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.get("/api/bills", headers=user_headers)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["error"] == "ACCOUNT_DISABLED"

    # Disabled user cannot login
    res = client.post("/api/auth/login", json={"email": "member@vib.com", "pin": "1234"})
    assert res.status_code == 403, res.text
    print("✓ Case 6 Passed: Disabled user gets 403 on existing session and login rejected")

    # Case 7: Admin enable lại user -> Login bình thường, role cũ giữ nguyên
    res = client.patch(f"/api/admin/users/{member_id}/status", json={"status": "active"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/login", json={"email": "member@vib.com", "pin": "1234"})
    assert res.status_code == 200, res.text
    assert res.json()["user"]["role"] == "user"
    assert res.json()["user"]["status"] == "active"
    print("✓ Case 7 Passed: Enabled user logs in with role and active status intact")

    print("\n🎉 ALL ADMIN SPEC TESTS COMPLETED 100% SUCCESSFULLY!")

if __name__ == "__main__":
    test_admin_flow()
