# Wi-Fi Remote

Control your Windows PC from your Android phone over your home Wi-Fi. You get a touchpad, a keyboard, a live view of the screen, and an assistant you can simply ask to open a folder, start a program or run a command.

## What you need

- A Windows 10 or 11 PC with Python 3.10 or newer (tick "Add to PATH" during setup)
- An Android phone connected to the same network

## Getting started

1. Run start_pc_remote.bat on the PC. A 6 digit pairing code shows up in the window.
2. Install WifiRemote.apk on your phone and open it.
3. Type in the code, then choose a PIN.

Phone can't see the PC? Run allow_firewall.bat once as administrator and try again.

## The assistant

The assistant works with an AI provider of your choice: DeepSeek, OpenAI, Gemini, Claude, or Ollama if you'd rather keep everything local. Add your API key from the app settings. The key stays on your PC and is never sent to the phone.

Anything that could do damage, like PowerShell commands, installs or admin actions, waits for your OK first.

## Files

- WifiRemote.apk is the phone app
- start_pc_remote.bat starts the PC side
- allow_firewall.bat opens the firewall ports if needed
- open_pairing_page.bat shows the pairing code in large digits
- server holds the PC program

## License

MIT


# بالعربي

برنامج يخليك تتحكم بكمبيوترك من جوالك وأنتم على نفس الواي فاي. تقدر تحرك الماوس وتكتب وتشوف الشاشة مباشرة، وتطلب من المساعد يفتح لك ملف أو برنامج أو ينفذ أمر بدون ما تقوم من مكانك.

## طريقة التشغيل

1. ثبت بايثون على الكمبيوتر (نسخة 3.10 أو أحدث) ولا تنس تختار Add to PATH وقت التثبيت.
2. شغل start_pc_remote.bat وبيطلع لك رقم اقتران من 6 أرقام.
3. ثبت WifiRemote.apk على جوالك، اكتب الرقم وبعدها اختر رمز PIN خاص فيك.

لو الجوال ما لقى الكمبيوتر، شغل allow_firewall.bat مرة وحدة كمسؤول.

## المساعد

عشان يشتغل المساعد أضف مفتاح API من أي مزود تبغاه من إعدادات التطبيق. المفتاح ينحفظ في كمبيوترك بس.

الأوامر الحساسة زي أوامر PowerShell والتثبيت ما تتنفذ إلا بعد موافقتك.

الجديد أنزله أول في حسابي على اكس: x.com/FahadPrimeXAPK
