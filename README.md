# Team Fund & Bill Split Web App

Hệ thống quản lý quỹ nhóm và chia hoá đơn nội bộ (Single Team / Single Group) đáp ứng đầy đủ BRD.

## Tính năng chính
- **Xác thực nhanh & an toàn:** Đăng nhập bằng Email + Mã PIN 4 số (bảo vệ bằng hash SHA-256 + salt). Đăng ký và Quên PIN qua mã OTP (kết nối webhook n8n hoặc dev code).
- **Quản lý Hoá đơn & Chia tiền:**
  - Nguồn tiền Cá nhân (Personal) hỗ trợ 3 chế độ chia: Chia đều (Mode A), Chia đều có chỉnh sửa (Mode B), Nhập tuỳ chỉnh (Mode C). Tự động khớp tổng VND (kiểu integer).
  - Nguồn tiền Quỹ nhóm (Group Fund): ghi nhận người tham gia, không tạo công nợ cá nhân, tự động trừ vào số dư quỹ.
  - Sửa / Xoá bill với cảnh báo reset trạng thái công nợ và đồng bộ quỹ.
- **Quy trình Công nợ & Thanh toán:**
  - Trạng thái: `UNPAID` -> `PAYMENT_REPORTED` (Tôi đã trả) -> `PAID` (Payer xác nhận đã nhận tiền).
  - Tích hợp tạo mã **VietQR** trực tiếp theo thông tin ngân hàng của Payer để debtor quét và chuyển khoản tiện lợi.
- **Sổ quỹ nhóm (Group Fund Ledger):**
  - Số dư quỹ = Tổng đóng quỹ - Tổng chi tiêu hoá đơn quỹ.
  - Thành viên tự ghi nhận đóng quỹ không cần phê duyệt rườm rà.
  - Bảng tổng kết đóng góp theo từng thành viên và lịch sử giao dịch.
- **Dashboard & Báo cáo:** Số dư quỹ, Tổng tiền tôi đang nợ, Tổng tiền người khác nợ tôi, hoá đơn gần đây, biến động quỹ.
- **Thông báo & Tự động hoá:** In-app notification realtime và phát sự kiện sang **n8n webhook** để gửi email/bot.

## Cài đặt & Khởi chạy

```bash
# Cài đặt dependencies (nếu chưa có)
pip install -r requirements.txt

# Khởi chạy ứng dụng
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Mở trình duyệt: `http://localhost:8000`

## Chạy kiểm thử tự động (17/17 tiêu chí BRD)

```bash
python3 test_app.py
```
