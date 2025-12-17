"""
🚀 نظام استضافة بوتات Telegram - Serverless على Vercel
الإصدار: 2.0 | مع التنظيف التلقائي وضمان الاستجابة الفورية
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import asyncio
import threading
import json
import os
import hashlib
import time
import psutil
import gc
from datetime import datetime, timedelta
from typing import Dict, Optional
import logging
import traceback
from functools import lru_cache

# ========== الإعدادات الأولية ==========
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# تكوين اللوج
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== محاكاة Redis في الذاكرة (للفرسل) ==========
class MemoryStorage:
    """تخزين في الذاكرة مع انتهاء الصلاحية التلقائي"""
    
    def __init__(self):
        self.data = {}
        self.expiry = {}
        self.stats = {
            'bots_created': 0,
            'bots_active': 0,
            'memory_usage': 0,
            'last_cleanup': time.time()
        }
    
    def set(self, key: str, value: any, ttl: int = None):
        """حفظ بيانات مع وقت انتهاء اختياري"""
        self.data[key] = value
        if ttl:
            self.expiry[key] = time.time() + ttl
        return True
    
    def get(self, key: str, default=None):
        """جلب بيانات مع التحقق من الصلاحية"""
        # حذف البيانات المنتهية
        if key in self.expiry and time.time() > self.expiry[key]:
            self.delete(key)
            return default
        
        return self.data.get(key, default)
    
    def delete(self, key: str):
        """حذف بيانات"""
        self.data.pop(key, None)
        self.expiry.pop(key, None)
        return True
    
    def keys(self, pattern: str = "*"):
        """الحصول على المفاتيح المتطابقة"""
        if pattern == "*":
            return list(self.data.keys())
        return [k for k in self.data.keys() if pattern in k]
    
    def exists(self, key: str):
        """التحقق من وجود المفتاح"""
        return key in self.data
    
    def incr(self, key: str):
        """زيادة قيمة رقمية"""
        val = int(self.get(key, 0))
        self.set(key, val + 1)
        return val + 1
    
    def cleanup_expired(self):
        """تنظيف البيانات المنتهية"""
        now = time.time()
        expired = [k for k, exp in self.expiry.items() if now > exp]
        for key in expired:
            self.delete(key)
        self.stats['last_cleanup'] = now
        return len(expired)

# إنشاء التخزين
storage = MemoryStorage()

# ========== مدير البوتات ==========
class BotManager:
    """إدارة وتشغيل البوتات"""
    
    def __init__(self):
        self.bots: Dict[str, any] = {}
        self.bot_tasks: Dict[str, asyncio.Task] = {}
        self.keep_alive_tasks = {}
        self.running = True
        
        # بدء خادم التنظيف التلقائي
        self.start_cleanup_daemon()
        self.start_keep_alive_daemon()
    
    def start_cleanup_daemon(self):
        """خادم تنظيف الذاكرة التلقائي"""
        def cleanup_worker():
            while self.running:
                try:
                    self.cleanup_memory()
                    gc.collect()  # تفعيل جامع القمامة
                    time.sleep(30)  # كل 30 ثانية
                except:
                    time.sleep(60)
        
        thread = threading.Thread(target=cleanup_worker, daemon=True)
        thread.start()
        logger.info("✅ بدأ خادم التنظيف التلقائي")
    
    def start_keep_alive_daemon(self):
        """خادم إبقاء البوتات نشطة"""
        def keep_alive_worker():
            while self.running:
                try:
                    self.ping_active_bots()
                    time.sleep(15)  # كل 15 ثانية
                except:
                    time.sleep(30)
        
        thread = threading.Thread(target=keep_alive_worker, daemon=True)
        thread.start()
        logger.info("✅ بدأ خادم إبقاء البوتات نشطة")
    
    def cleanup_memory(self):
        """تنظيف الذاكرة وحذف البوتات غير النشطة"""
        try:
            # حذف البوتات غير النشطة لأكثر من ساعة
            cutoff = time.time() - 3600
            inactive_bots = []
            
            for bot_token in list(self.bots.keys()):
                last_active = storage.get(f"bot:{bot_token}:last_active", 0)
                if last_active < cutoff:
                    inactive_bots.append(bot_token)
            
            for bot_token in inactive_bots:
                self.stop_bot(bot_token)
                logger.info(f"🧹 تم تنظيف البوت غير النشط: {bot_token[:10]}...")
            
            # تنظيف التخزين
            expired = storage.cleanup_expired()
            if expired > 0:
                logger.info(f"🧹 تم تنظيف {expired} عنصر منتهي الصلاحية")
            
            # إحصاءات الذاكرة
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            storage.stats['memory_usage'] = round(memory_mb, 2)
            storage.stats['bots_active'] = len(self.bots)
            
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في التنظيف: {e}")
            return False
    
    def ping_active_bots(self):
        """إرسال إشارات حياة للبوتات النشطة"""
        for bot_token in list(self.bots.keys()):
            try:
                # تحديث وقت النشاط الأخير
                storage.set(f"bot:{bot_token}:last_active", time.time())
                
                # إرسال إشارة حياة (ping) إذا كان البوت يدعمها
                if storage.get(f"bot:{bot_token}:status") == "active":
                    # يمكن إضافة إشارات حياة مخصصة هنا
                    pass
            except:
                pass
    
    async def create_bot_instance(self, token: str, code: str):
        """إنشاء مثيل بوت وتشغيله"""
        try:
            from telegram.ext import Application, MessageHandler, filters
            from telegram import Update
            import asyncio
            
            # إنشاء تطبيق البوت
            application = Application.builder().token(token).build()
            
            # تجميع كود البوت في دالة
            bot_code = f"""
async def user_bot_main(update: Update, context):
    try:
        {code}
    except Exception as e:
        print(f"خطأ في بوت المستخدم: {{e}}")
"""
            
            # تنفيذ الكود وإنشاء الدالة
            exec_globals = {
                'Update': Update,
                'filters': filters,
                'asyncio': asyncio,
                'print': print,
                'json': json
            }
            exec(bot_code, exec_globals)
            
            # تسجيل المعالج
            user_handler = exec_globals['user_bot_main']
            application.add_handler(MessageHandler(filters.ALL, user_handler))
            
            # بدء البوت (Webhook mode لـ Serverless)
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            
            return application
        except Exception as e:
            logger.error(f"❌ خطأ في إنشاء البوت: {e}")
            return None
    
    def start_bot(self, token: str, code: str):
        """بدء تشغيل بوت جديد"""
        try:
            # إذا كان البوت يعمل بالفعل، إيقافه أولاً
            if token in self.bots:
                self.stop_bot(token)
            
            # تخزين كود البوت
            storage.set(f"bot:{token}:code", code)
            storage.set(f"bot:{token}:status", "active")
            storage.set(f"bot:{token}:last_active", time.time())
            storage.set(f"bot:{token}:created_at", time.time())
            storage.incr("stats:bots_created")
            
            # تشغيل البوت في thread منفصل
            async def run_bot():
                try:
                    bot_instance = await self.create_bot_instance(token, code)
                    if bot_instance:
                        self.bots[token] = bot_instance
                        logger.info(f"✅ بدأ تشغيل البوت: {token[:10]}...")
                        
                        # إبقاء البوت نشطاً
                        while token in self.bots:
                            await asyncio.sleep(1)
                    else:
                        storage.set(f"bot:{token}:status", "error")
                except Exception as e:
                    logger.error(f"❌ خطأ في تشغيل البوت: {e}")
                    storage.set(f"bot:{token}:status", "error")
            
            # تشغيل في event loop منفصل
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(run_bot())
            
            # تشغيل الـ loop في thread
            def start_loop():
                asyncio.set_event_loop(loop)
                loop.run_forever()
            
            thread = threading.Thread(target=start_loop, daemon=True)
            thread.start()
            
            self.bot_tasks[token] = task
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في بدء البوت: {e}")
            storage.set(f"bot:{token}:status", "error")
            return False
    
    def stop_bot(self, token: str):
        """إيقاف بوت"""
        try:
            if token in self.bots:
                # إيقاف البوت
                bot_instance = self.bots[token]
                asyncio.run(bot_instance.stop())
                asyncio.run(bot_instance.shutdown())
                
                # تنظيف
                del self.bots[token]
                if token in self.bot_tasks:
                    self.bot_tasks[token].cancel()
                    del self.bot_tasks[token]
            
            storage.set(f"bot:{token}:status", "stopped")
            logger.info(f"⏸️ تم إيقاف البوت: {token[:10]}...")
            return True
        except:
            storage.set(f"bot:{token}:status", "stopped")
            return True
    
    def delete_bot(self, token: str):
        """حذف بوت نهائياً"""
        self.stop_bot(token)
        
        # حذف جميع بيانات البوت
        keys = storage.keys(f"bot:{token}:*")
        for key in keys:
            storage.delete(key)
        
        storage.delete(f"webhook:{token}")
        logger.info(f"🗑️ تم حذف البوت: {token[:10]}...")
        return True
    
    def get_bot_info(self, token: str):
        """الحصول على معلومات البوت"""
        info = {
            'status': storage.get(f"bot:{token}:status", "not_found"),
            'created_at': storage.get(f"bot:{token}:created_at"),
            'last_active': storage.get(f"bot:{token}:last_active"),
            'memory_used': storage.get(f"bot:{token}:memory", 0),
            'is_running': token in self.bots
        }
        return info
    
    def get_all_bots(self):
        """الحصول على قائمة جميع البوتات"""
        bots = []
        for key in storage.keys("bot:*:status"):
            token = key.split(":")[1]
            bots.append({
                'token': token[:10] + "...",
                'status': storage.get(key),
                'created_at': storage.get(f"bot:{token}:created_at")
            })
        return bots

# إنشاء مدير البوتات
bot_manager = BotManager()

# ========== واجهات API ==========

@app.route('/')
def home():
    """الصفحة الرئيسية"""
    return jsonify({
        "status": "running",
        "service": "Telegram Bot Hosting Platform",
        "version": "2.0",
        "bots_active": len(bot_manager.bots),
        "memory_usage": storage.stats['memory_usage'],
        "uptime": time.time() - storage.stats['last_cleanup']
    })

@app.route('/api/upload', methods=['POST'])
def upload_bot():
    """رفع وتشغيل بوت جديد"""
    try:
        data = request.json
        token = data.get('token', '').strip()
        code = data.get('code', '').strip()
        
        if not token or not code:
            return jsonify({
                "success": False,
                "message": "❌ يرجى إدخال التوكن والكود"
            })
        
        # التحقق من صحة التوكن
        if not token.startswith('') or ':' not in token:
            return jsonify({
                "success": False,
                "message": "❌ توكن غير صالح. تأكد من صحة التوكن"
            })
        
        # بدء تشغيل البوت
        success = bot_manager.start_bot(token, code)
        
        if success:
            return jsonify({
                "success": True,
                "message": "✅ تم رفع وتشغيل البوت بنجاح!",
                "bot_id": hashlib.md5(token.encode()).hexdigest()[:8],
                "webhook_url": f"{request.host_url}webhook/{token}"
            })
        else:
            return jsonify({
                "success": False,
                "message": "❌ فشل في تشغيل البوت. تأكد من صحة الكود"
            })
            
    except Exception as e:
        logger.error(f"❌ خطأ في رفع البوت: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في الخادم: {str(e)}"
        })

@app.route('/api/control', methods=['POST'])
def control_bot():
    """التحكم في البوت (تشغيل/إيقاف/حذف)"""
    try:
        data = request.json
        token = data.get('token', '').strip()
        action = data.get('action', '').lower()
        
        if not token:
            return jsonify({
                "success": False,
                "message": "❌ يرجى إدخال توكن البوت"
            })
        
        if action == 'start':
            # جلب الكود من التخزين وإعادة التشغيل
            code = storage.get(f"bot:{token}:code")
            if not code:
                return jsonify({
                    "success": False,
                    "message": "❌ لا يوجد كود محفوظ لهذا البوت"
                })
            
            bot_manager.start_bot(token, code)
            return jsonify({
                "success": True,
                "message": "✅ تم تشغيل البوت بنجاح"
            })
        
        elif action == 'stop':
            bot_manager.stop_bot(token)
            return jsonify({
                "success": True,
                "message": "⏸️ تم إيقاف البوت مؤقتاً"
            })
        
        elif action == 'delete':
            bot_manager.delete_bot(token)
            return jsonify({
                "success": True,
                "message": "🗑️ تم حذف البوت نهائياً"
            })
        
        elif action == 'status':
            info = bot_manager.get_bot_info(token)
            return jsonify({
                "success": True,
                "status": info
            })
        
        else:
            return jsonify({
                "success": False,
                "message": "❌ إجراء غير معروف"
            })
            
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """الحصول على إحصائيات النظام"""
    return jsonify({
        "success": True,
        "stats": {
            "bots_created": storage.stats['bots_created'],
            "bots_active": len(bot_manager.bots),
            "memory_usage_mb": storage.stats['memory_usage'],
            "storage_items": len(storage.data),
            "uptime_seconds": time.time() - storage.stats['last_cleanup'],
            "last_cleanup": datetime.fromtimestamp(storage.stats['last_cleanup']).isoformat()
        }
    })

@app.route('/api/list', methods=['GET'])
def list_bots():
    """قائمة البوتات"""
    bots = bot_manager.get_all_bots()
    return jsonify({
        "success": True,
        "bots": bots,
        "count": len(bots)
    })

@app.route('/webhook/<token>', methods=['POST'])
def webhook_handler(token):
    """معالجة ويب هوك البوتات"""
    try:
        # التحقق من وجود البوت ونشاطه
        status = storage.get(f"bot:{token}:status")
        if status != "active":
            return jsonify({"ok": False, "error": "Bot not active"})
        
        # تحديث وقت النشاط الأخير
        storage.set(f"bot:{token}:last_active", time.time())
        
        # معالجة التحديث (هنا يمكن إضافة معالجة مخصصة)
        update = request.json
        
        # حفظ آخر تحديث للتصحيح
        storage.set(f"bot:{token}:last_update", json.dumps(update), ttl=300)
        
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.error(f"❌ خطأ في ويب هوك: {e}")
        return jsonify({"ok": False, "error": str(e)})

@app.route('/api/cleanup', methods=['POST'])
def manual_cleanup():
    """تنظيف يدوي للنظام"""
    try:
        # تنظيف الذاكرة
        cleaned = bot_manager.cleanup_memory()
        
        # تفعيل جامع القمامة
        gc.collect()
        
        return jsonify({
            "success": True,
            "message": "🧹 تم تنظيف النظام بنجاح",
            "memory_freed": "✓",
            "bots_stopped": cleaned if isinstance(cleaned, int) else "N/A"
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في التنظيف: {str(e)}"
        })

# ========== ملفات إضافية للويب ==========

@app.route('/dashboard')
def dashboard():
    """لوحة التحكم"""
    return send_file('index.html')

# ========== التشغيل ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
else:
    # للتشغيل على Vercel
    application = app