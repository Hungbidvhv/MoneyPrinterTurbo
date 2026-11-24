# Một video thật trên đám mây: kiểm thử tích hợp, không phải dịch vụ dựng miễn phí

Gói này kiểm tra bản phần mềm cá nhân nối n8n → Video Studio → MoneyPrinterTurbo. Chỉ chạy một công việc với nội dung minh họa công khai, không lấy bất kỳ ảnh/video/tài liệu cá nhân nào từ máy Surface.

## Sẵn sàng để bạn xem xét trước khi công khai

Dự kiến thêm vào nhánh riêng `video-studio-cloud-trial` của kho công khai `Hungbidvhv/MoneyPrinterTurbo`, giữ nguyên nhánh main:

- `video-studio/studio.py`: backend tích hợp nhỏ, không kèm dữ liệu hay cấu hình cá nhân.
- `video-studio/cloud/run_cloud_trial.py`: phép thử đầu-cuối chạy trên Linux của GitHub, từ chối chạy dựng trên Windows.
- `video-studio/cloud/make_trial_art.py`: tạo ba hình minh họa gốc trên runner.
- `video-studio/cloud/README-CLOUD.md`: tài liệu này.
- `.github/workflows/video-studio-trial.yml`: workflow kiểm thử có giới hạn thời gian.

Không đưa `.env`, `.private`, cơ sở dữ liệu, lịch sử, nhật ký máy hoặc bất kỳ tệp sẵn có trên ổ C/D lên GitHub. Mã kiểm thử và video mẫu có thể được người khác truy cập vì kho là công khai. Tệp cục bộ chưa được đăng cho đến khi chủ tài khoản đồng ý.

## Nội dung và phí

Kịch bản: ba thói quen tiết kiệm điện, được viết sẵn trong mã kiểm thử; không gọi AI tạo nội dung. Ba hình minh họa được vẽ bằng Pillow. Giọng đọc tiếng Việt dùng Edge TTS mà MoneyPrinterTurbo tích hợp. Không cần khóa Pexels, OpenAI hoặc bất kỳ LLM trả phí nào. Sau này tự động viết chủ đề mới vẫn cần thêm dịch vụ AI phù hợp, không được coi là đã kiểm chứng trong phép thử này.

Runner chuẩn Ubuntu của kho công khai được GitHub miễn phí theo tài liệu hiện tại. Không dùng larger runner, máy chủ thuê hoặc lịch chạy định kỳ. Chỉ một job, tối đa 35 phút; một video tối đa 25 MB. Artifact lưu 1 ngày và dùng chung hạn mức lưu trữ của tài khoản; phải kiểm tra hạn mức còn lại trước khi bật lưu artifact nếu tài khoản đang gần đầy. Không bật chi phí vượt hạn mức hoặc thay đổi thanh toán.

Đây là việc **kiểm thử ứng dụng liên quan đến kho mã**, phù hợp mục đích phát triển/kiểm thử của Actions. Không biến Actions thành backend sản xuất hàng loạt, máy chủ web hay dịch vụ dựng video liên tục. Máy Surface không cài Docker, không tải mô hình hoặc dựng video trong quá trình này; chỉ tải video kết quả khi kiểm thử xong.

## Cách chạy sau khi được đồng ý

1. Thêm đúng các tệp trong danh sách cho phép ở trên vào nhánh riêng. Không sửa workflow upstream có sẵn.
2. Nếu GitHub đang vô hiệu hóa Actions của bản Fork, chủ tài khoản cần mở tab Actions và bật workflow cho bản Fork. Không cần nhập thông tin thanh toán.
3. Push vào nhánh thử sẽ kích hoạt đúng workflow mới nếu Actions đã được bật. Nếu chưa bật, có thể cần một commit mới trên nhánh sau khi bật. Không cần merge main để chạy trigger push.
4. Workflow tải hai image chính thức xuống runner, khởi động API MPT, tạo hình minh họa, khởi động backend và tạo một việc với kịch bản có sẵn.
5. Workflow import credential ngẫu nhiên và workflow n8n trong môi trường tạm. n8n thực sự gọi từng bước, chờ dựng và đọc kết quả; không dùng bộ giả lập để báo video thật.
6. Chỉ khi backend xác nhận hoàn tất và FFmpeg giải mã được cả hình + âm thanh, job mới được coi là đạt. Kết quả `video-test.mp4` và `result.json` là hai tệp duy nhất được xuất.

Giọng đọc trực tuyến có thể bị giới hạn hoặc không truy cập được từ runner. Khi đó phải sửa kết nối và chạy lại kiểm thử chủ động, không giả báo thành công. Image MPT hiện dùng tag chính thức `latest`; kết quả ghi image ID thực tế để đối chiếu và khóa digest sau lần kiểm thử đạt.

## Đã kiểm tra / chưa kiểm tra

- Mã Python và bộ thử cục bộ kiểm tra kết nối local-material, từ chối chạy trên Surface và bảo vệ dữ liệu.
- Chưa chạy Docker/n8n/MPT thật trên runner, chưa có MP4 thật cho đến khi đăng bộ kiểm thử và chạy được trên GitHub.
- Chưa triển khai dịch vụ hoạt động 24/7. Hàng đợi demo trên Surface vẫn ở demo và không tự đổi sang chế độ tính phí.

Nguồn: https://docs.github.com/en/billing/concepts/product-billing/github-actions ; https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions ; https://github.com/harry0703/MoneyPrinterTurbo
