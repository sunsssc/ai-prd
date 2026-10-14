import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_internal_notice(subject, body, to_emails):
    # Gmail SMTP 配置
    smtp_server = "smtp.gmail.com"
    smtp_port = 465  # 465 是 SSL 端口，587 是 TLS 端口

    sender_email = os.environ["GMAIL_SENDER"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    # 构建邮件对象
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = ", ".join(to_emails)  # 支持群发，逗号分隔
    msg['Subject'] = subject

    # 附加邮件正文 (plain 表示纯文本，如果发 HTML 就用 'html')
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        # 建立安全连接并发送
        print("正在连接 SMTP 服务器...")
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, to_emails, msg.as_string())
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败: {e}")

# 测试调用
if __name__ == "__main__":
    target_emails = ["jordan.lee@corp.test"]
    test_subject = "【系统通知】内部服务状态巡检"
    test_body = "这是一封由脚本自动发出的测试邮件，请确认是否收到。"
    
    send_internal_notice(test_subject, test_body, target_emails)