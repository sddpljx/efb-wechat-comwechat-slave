import logging, tempfile
import time
import threading
from traceback import print_exc
from pydub import AudioSegment
import qrcode
from pyzbar.pyzbar import decode as pyzbar_decode
import os
import base64
from pathlib import Path
from xml.sax.saxutils import escape

import re
import json
from ehforwarderbot.chat import SystemChat, PrivateChat , SystemChatMember, ChatMember, SelfChatMember
import hashlib
from typing import Tuple, Optional, Collection, BinaryIO, Dict, Any , Union , List
from datetime import datetime
from cachetools import TTLCache

from ehforwarderbot import MsgType, Chat, Message, Status, coordinator
from wechatrobot import WeChatRobot

from . import __version__ as version

from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.types import MessageID, ChatID, InstanceID
from ehforwarderbot import utils as efb_utils
from ehforwarderbot.exceptions import EFBException, EFBChatNotFound, EFBMessageError
from ehforwarderbot.message import MessageCommand, MessageCommands
from ehforwarderbot.status import MessageRemoval

from .ChatMgr import ChatMgr
from .CustomTypes import EFBGroupChat, EFBPrivateChat, EFBGroupMember, EFBSystemUser
from .MsgDeco import qutoed_text
from .MsgProcess import MsgProcess, MsgWrapper
from .Utils import download_file , load_config , load_temp_file_to_local , WC_EMOTICON_CONVERSION
from .Constant import QUOTE_MESSAGE

from rich.console import Console
from rich import print as rprint
from io import BytesIO
from PIL import Image
from pyqrcode import QRCode

class ComWeChatChannel(SlaveChannel):
    channel_name : str = "ComWechatChannel"
    channel_emoji : str = "💻"
    channel_id : str = "honus.comwechat"

    bot : WeChatRobot = None
    config : Dict = {}

    friends : EFBPrivateChat = []
    groups : EFBGroupChat    = []

    contacts : Dict = {}            # {wxid : {alias : str , remark : str, nickname : str , type : int}} -> {wxid : name(after handle)}
    group_members : Dict = {}       # {"group_id" : { "wxID" : "displayName"}}

    time_out : int = 120
    cache =  TTLCache(maxsize=200, ttl= time_out)  # 缓存发送过的消息ID
    file_msg : Dict = {}                           # 存储待修改的文件类消息 {path : msg}
    delete_file : Dict = {}                        # 存储待删除的消息 {path : time}
    forward_pattern = r"ehforwarderbot:\/\/([^/]+)\/forward\/(\d+)"

    __version__ = version.__version__
    logger: logging.Logger = logging.getLogger("comwechat")
    logger.setLevel(logging.DEBUG)

    #MsgType.Voice
    supported_message_types = {MsgType.Text, MsgType.Sticker, MsgType.Image , MsgType.Link , MsgType.File , MsgType.Video , MsgType.Animation, MsgType.Voice}

    def __init__(self, instance_id: InstanceID = None):
        super().__init__(instance_id=instance_id)
        self.logger.info("ComWeChat Slave Channel initialized.")
        self.logger.info("Version: %s" % self.__version__)
        self.config = load_config(efb_utils.get_config_path(self.channel_id))
        self.bot = WeChatRobot()

        self.qr_url = ""
        self.master_qr_picture_id: Optional[str] = None
        self.user_auth_chat = SystemChat(channel=self,
                                    name="EWS User Auth",
                                    uid=ChatID("__ews_user_auth__"))

        self.qrcode_timeout = self.config.get("qrcode_timeout", 10)
        self.login()
        self.me = self.bot.GetSelfInfo()["data"]
        self.wxid = self.me["wxId"]
        self.base_path = self.config["base_path"] if "base_path" in self.config else self.bot.get_base_path()
        self.dir = self.config["dir"]
        if not self.dir.endswith(os.path.sep):
            self.dir += os.path.sep
        
        try:
            import subprocess
            import json
            
            url = 'http://127.0.0.1:18888/api/?type=35'
            payload = {'version': '3.9.12.55'}
            payload_str = json.dumps(payload)
            
            self.logger.info(f"向Hook发送微信版本号: {payload['version']}")
            cmd = ["curl", "-X", "POST", url, "-d", payload_str]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode != 0:
                self.logger.error(f"设置微信版本号的curl命令执行失败. Curl stderr: {result.stderr.strip()}")
            else:
                try:
                    response = json.loads(result.stdout)
                    # Assuming a response with 'result' == 'OK' indicates success.
                    if response.get('result') == 'OK':
                        self.logger.info("成功设置微信版本号.")
                    else:
                        self.logger.error(f"设置微信版本号失败，Hook返回: {result.stdout.strip()}")
                except json.JSONDecodeError:
                    self.logger.error(f"解析Hook返回的JSON失败. Response: {result.stdout.strip()}")
                    
        except Exception as e:
            self.logger.error(f"设置微信版本号失败: {e}")

        # WSL环境检测和路径转换配置
        self.is_wsl = self._detect_wsl()
        if self.is_wsl:
            self.logger.info("检测到WSL环境，启用WSL到Windows路径转换")
            try:
                import subprocess
                import json

                # 移除末尾的路径分隔符
                clean_dir = self.dir.rstrip(os.path.sep)
                win_path = self._wsl_to_windows_path(clean_dir)

                payload = {"save_path": win_path}
                payload_str = json.dumps(payload)

                # 设置图片保存路径 (type=13)
                url13 = 'http://127.0.0.1:18888/api/?type=13'
                self.logger.info(f"向Hook发送图片保存路径: {win_path}")
                cmd13 = ["curl", "-X", "POST", url13, "-d", payload_str]
                result13 = subprocess.run(cmd13, capture_output=True, text=True, timeout=5)
                if result13.returncode != 0:
                    self.logger.error(f"设置图片保存路径的curl命令执行失败. Curl stderr: {result13.stderr.strip()}")
                else:
                    try:
                        response = json.loads(result13.stdout)
                        if response.get('msg') == 1 and response.get('result') == 'OK':
                            self.logger.info("成功设置Hook图片保存路径.")
                        else:
                            self.logger.error(f"设置Hook图片保存路径失败，Hook返回: {result13.stdout.strip()}")
                    except json.JSONDecodeError:
                        self.logger.error(f"解析Hook返回的JSON失败. Response: {result13.stdout.strip()}")

                # 设置语音保存路径 (type=11)
                url11 = 'http://127.0.0.1:18888/api/?type=11'
                self.logger.info(f"向Hook发送语音保存路径: {win_path}")
                cmd11 = ["curl", "-X", "POST", url11, "-d", payload_str]
                result11 = subprocess.run(cmd11, capture_output=True, text=True, timeout=5)
                if result11.returncode != 0:
                    self.logger.error(f"设置语音保存路径的curl命令执行失败. Curl stderr: {result11.stderr.strip()}")
                else:
                    try:
                        response = json.loads(result11.stdout)
                        if response.get('msg') == 1 and response.get('result') == 'OK':
                            self.logger.info("成功设置Hook语音保存路径.")
                        else:
                            self.logger.error(f"设置Hook语音保存路径失败，Hook返回: {result11.stdout.strip()}")
                    except json.JSONDecodeError:
                        self.logger.error(f"解析Hook返回的JSON失败. Response: {result11.stdout.strip()}")

            except Exception as e:
                self.logger.error(f"设置Windows Hook路径失败: {e}")
        
        ChatMgr.slave_channel = self

        @self.bot.on("self_msg")
        def on_self_msg(msg : Dict):
            self.logger.debug(f"self_msg:{msg}")
            sender = msg["sender"]

            name = self.get_name_by_wxid(sender)

            if "@chatroom" in sender:
                chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                    uid = sender,
                    name = name,
                ))
                author = chat.self
            else:
                chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid = sender,
                    name = name,
                ))
                if sender.startswith('gh_'):
                    chat.vendor_specific = {'is_mp' : True}
                author = chat.self

            self.handle_msg(msg , author , chat)

        @self.bot.on("friend_msg")
        def on_friend_msg(msg : Dict):
            self.logger.debug(f"friend_msg:{msg}")

            sender = msg['sender']

            if msg["type"] == "eventnotify":
                return

            name = self.get_name_by_wxid(sender)

            chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid= sender,
                    name= name,
            ))
            if sender.startswith('gh_'):
                chat.vendor_specific = {'is_mp' : True}
                self.logger.debug(f'modified_chat:{chat}')
            author = chat.other
            self.handle_msg(msg, author, chat)

        @self.bot.on("group_msg")
        def on_group_msg(msg : Dict):
            self.logger.debug(f"group_msg:{msg}")
            sender = msg["sender"]
            wxid  =  msg["wxid"]

            chatname = self.get_name_by_wxid(sender)

            chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                uid = sender,
                name = chatname,
            ))

            try:
                name = self.contacts[wxid]
            except:
                name = wxid

            author = ChatMgr.build_efb_chat_as_member(chat, EFBGroupMember(
                uid = wxid,
                name = name,
                alias = self.group_members.get(sender,{}).get(wxid , None),
            ))
            self.handle_msg(msg, author, chat)

        @self.bot.on("revoke_msg")
        def on_revoked_msg(msg : Dict):
            self.logger.debug(f"revoke_msg:{msg}")
            sender = msg["sender"]
            if "@chatroom" in sender:
                wxid  =  msg["wxid"]

            name = self.get_name_by_wxid(sender)

            if "@chatroom" in sender:
                chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                    uid = sender,
                    name = name,
                ))
            else:
                chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid = sender,
                    name = name,
                ))

            newmsgid = re.search("<newmsgid>(.*?)<\/newmsgid>", msg["message"]).group(1)

            efb_msg = Message(chat = chat , uid = newmsgid)
            coordinator.send_status(
                MessageRemoval(source_channel=self, destination_channel=coordinator.master, message=efb_msg)
            )

        @self.bot.on("transfer_msg")
        def on_transfer_msg(msg : Dict):
            self.logger.debug(f"transfer_msg:{msg}")
            sender = msg["sender"]
            name = self.get_name_by_wxid(sender)

            if msg["isSendMsg"]:
                if msg["isSendByPhone"]:
                    chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                            uid= sender,
                            name= name,
                    ))
                    author = chat.other
                    self.handle_msg(msg, author, chat)
                    return

            content = {}

            money = re.search("收到转账(.*)元", msg["message"]).group(1)
            transcationid = re.search("<transcationid><!\[CDATA\[(.*)\]\]><\/transcationid>", msg["message"]).group(1)
            transferid = re.search("<transferid><!\[CDATA\[(.*)\]\]><\/transferid>", msg["message"]).group(1)
            text = (
                f"收到 {name} 转账:\n"
                f"金额为 {money} 元\n"
            )

            commands = [
                MessageCommand(
                    name=("Accept"),
                    callable_name="process_transfer",
                    kwargs={"transcationid" : transcationid , "transferid" : transferid , "wxid" : sender},
                )
            ]

            content["sender"] = sender
            content["message"] = text
            content["commands"] = commands
            content["name"] = name
            self.system_msg(content)

        @self.bot.on("frdver_msg")
        def on_frdver_msg(msg : Dict):
            self.logger.debug(f"frdver_msg:{msg}")
            content = {}
            sender = msg["sender"]
            fromnickname = re.search('fromnickname="(.*?)"', msg["message"]).group(1)
            apply_content = re.search('content="(.*?)"', msg["message"]).group(1)
            url = re.search('bigheadimgurl="(.*?)"', msg["message"]).group(1)
            v3 = re.search('encryptusername="(v3.*?)"', msg["message"]).group(1)
            v4 = re.search('ticket="(v4.*?)"', msg["message"]).group(1)
            text = (
                "好友申请:\n"
                f"名字: {fromnickname}\n"
                f"验证内容: {apply_content}\n"
                f"头像: {url}"
            )

            commands = [
                MessageCommand(
                    name=("Accept"),
                    callable_name="process_friend_request",
                    kwargs={"v3" : v3 , "v4" : v4},
                )
            ]

            content["sender"] = sender
            content["message"] = text
            content["commands"] = commands
            self.system_msg(content)

        @self.bot.on("card_msg")
        def on_card_msg(msg : Dict):
            self.logger.debug(f"card_msg:{msg}")
            sender = msg["sender"]
            wxid = msg["wxid"]
            content = {}
            name = self.get_name_by_wxid(sender)

            bigheadimgurl = re.search('bigheadimgurl="(.*?)"', msg["message"]).group(1)
            nickname = re.search('nickname="(.*?)"', msg["message"]).group(1)
            province = re.search('province="(.*?)"', msg["message"]).group(1)
            city = re.search('city="(.*?)"', msg["message"]).group(1)
            sex = re.search('sex="(.*?)"', msg["message"]).group(1)
            username = re.search('username="(.*?)"', msg["message"]).group(1)

            text = "名片信息:\n"
            if nickname:
                text += f"昵称: {nickname}\n"
            if city:
                text += f"城市: {city}\n"
            if province:
                text += f"省份: {province}\n"
            if sex:
                if sex == "0":
                    text += "性别: 未知\n"
                elif sex == "1":
                    text += "性别: 男\n"
                elif sex == "2":
                    text += "性别: 女\n"
            if bigheadimgurl:
                text += f"头像: {bigheadimgurl}\n"

            commands = [
                MessageCommand(
                    name=("Add To Friend"),
                    callable_name="add_friend",
                    kwargs={"v3" : username},
                )
            ]

            content["sender"] = sender
            content["message"] = text
            content["name"] = name
            # if "v3" in username:
            #     content["commands"] = commands
            # 暂时屏蔽
            self.system_msg(content)

    def login(self):
        self.master_qr_picture_id = None
        # 每隔 10 秒检查一次登录状态
        while True:
            try:
                response = self.bot.IsLoginIn()
                if response.get("is_login", 0) == 1:
                    print(f"登录成功", flush=True)
                    break
                
                # 获取二维码并检查返回结果
                if self.get_qrcode():
                    print(f"已经登录", flush=True)
                    break
                    
            except Exception as e:
                self.logger.error(f"登录出错: {str(e)}")
                pass
                
            time.sleep(self.qrcode_timeout)

    def get_qrcode(self):
        result = self.bot.GetQrcodeImage()
        
        # 检查是否返回了 JSON 数据（已登录）
        try:
            json_result = json.loads(result)
            if isinstance(json_result, dict):
                if json_result.get("result") == "OK":
                    return True
        except Exception:
            pass
            
        file = self.save_qr_code(result)
        if not file:
            return False
            
        url = self.decode_qr_code(file)
        if not url:
            os.unlink(file.name)  # 删除临时文件
            return False
            
        if self.qr_url != url:
            self.qr_url = url
            self.console_qr_code(url)
            # self.master_qr_code(file)
            
        # 在使用完成后删除临时文件
        os.unlink(file.name)
        return False

    @staticmethod
    def save_qr_code(qr_code):
        # 创建临时文件保存二维码图片
        tmp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        try:
            tmp_file.write(qr_code)
            tmp_file.flush()
        except:
            print("[red]获取二维码失败[/red]", flush=True)
            tmp_file.close()
            return None
        tmp_file.close()
        return tmp_file

    @staticmethod
    def decode_qr_code(file):
        # 从临时文件读取图片并解码二维码数据
        qr_img = Image.open(file.name)
        try:
            return pyzbar_decode(qr_img)[0].data.decode('utf-8')
        except IndexError:
            # 如果解码失败，直接使用图片数据
            print("[yellow]无法解析二维码数据，但二维码图片已保存[/yellow]", flush=True)

    @staticmethod
    def console_qr_code(url):
        # 使用 qrcode 创建一个新的二维码实例
        qr = qrcode.QRCode(
            version=None,  # 自动选择合适的版本
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,    # 每个 QR 模块的像素大小
            border=1       # 二维码边框大小
        )
        qr.add_data(url)
        qr.make(fit=True)  # 自动调整大小
        
        # 使用 rich 打印彩色提示
        console = Console()
        console.print("\n[bold green]请扫描以下二维码登录微信：[/bold green]")
        # 在终端打印二维码
        qr.print_ascii(invert=True)

    # TODO master 还未初始化
    # def master_qr_code(self, file):
    #     msg = Message(
    #         type=MsgType.Text,
    #         chat=self.user_auth_chat,
    #         author=self.user_auth_chat.other,
    #         deliver_to=coordinator.master,
    #     )
    #     msg.type = MsgType.Image
    #     msg.text = self._("QR code expired, please scan the new one.")
    #     msg.path = Path(file.name)
    #     msg.file = file
    #     msg.mime = 'image/png'
    #     if self.master_qr_picture_id is not None:
    #         msg.edit = True
    #         msg.edit_media = True
    #         msg.uid = self.master_qr_picture_id
    #     else:
    #         self.master_qr_picture_id = msg.uid
    #     coordinator.send_message(msg)

    @staticmethod
    def send_efb_msgs(efb_msgs: Union[Message, List[Message]], **kwargs):
        if not efb_msgs:
            return
        efb_msgs = [efb_msgs] if isinstance(efb_msgs, Message) else efb_msgs
        if 'deliver_to' not in kwargs:
            kwargs['deliver_to'] = coordinator.master
        for efb_msg in efb_msgs:
            for k, v in kwargs.items():
                setattr(efb_msg, k, v)
            coordinator.send_message(efb_msg)
            if efb_msg.file:
                efb_msg.file.close()

    def system_msg(self, content : Dict):
        self.logger.debug(f"system_msg:{content}")
        msg = Message()
        sender = content["sender"]
        if "name" in content:
            name = content["name"]
        else:
            name  = '\u2139 System'

        chat = ChatMgr.build_efb_chat_as_system_user(EFBSystemUser(
            uid = sender,
            name = name
        ))

        try:
            author = chat.get_member(SystemChatMember.SYSTEM_ID)
        except KeyError:
            author = chat.add_system_member()

        if "commands" in content:
            msg.commands = MessageCommands(content["commands"])
        if "message" in content:
            msg.text = content['message']
        if "target" in content:
            msg.target = content['target']

        self.send_efb_msgs(msg, uid=int(time.time()), chat=chat, author=author, type=MsgType.Text)

    def handle_msg(self , msg : Dict[str, Any] , author : 'ChatMember' , chat : 'Chat'):
        emojiList = re.findall('\[[\w|！|!| ]+\]' , msg["message"])
        for emoji in emojiList:
            try:
                msg["message"] = msg["message"].replace(emoji, WC_EMOTICON_CONVERSION[emoji])
            except:
                pass

        if msg["msgid"] not in self.cache:
            self.cache[msg["msgid"]] = msg["type"]
        else:
            if self.cache[msg["msgid"]] == msg["type"]:
                return

        try:
            if ("FileStorage" in msg["filepath"]) and ("Cache" not in msg["filepath"]):
                msg["timestamp"] = int(time.time())
                msg["filepath"] = msg["filepath"].replace("\\","/")
                msg["filepath"] = f'''{self.dir}{msg["filepath"]}'''
                self.file_msg[msg["filepath"]] = ( msg , author , chat )
                return
            if msg["type"] == "video":
                msg["timestamp"] = int(time.time())
                msg["filepath"] = msg["thumb_path"].replace("\\","/").replace(".jpg", ".mp4")
                msg["filepath"] = f'''{self.dir}{msg["filepath"]}'''
                self.file_msg[msg["filepath"]] = ( msg , author , chat )
                return
        except:
            ...

        if msg["type"] == "voice":
            file_path = re.search("clientmsgid=\"(.*?)\"", msg["message"]).group(1) + ".amr"
            msg["timestamp"] = int(time.time())
            msg["filepath"] = f'''{self.dir}{msg["self"]}/{file_path}'''
            self.file_msg[msg["filepath"]] = ( msg , author , chat )
            return

        self.send_efb_msgs(MsgWrapper(msg, MsgProcess(msg, chat)), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))

    def handle_file_msg(self):
        while True:
            if len(self.file_msg) == 0:
                time.sleep(1)
            else:
                for path in list(self.file_msg.keys()):
                    flag = False
                    msg = self.file_msg[path][0]
                    author = self.file_msg[path][1]
                    chat = self.file_msg[path][2]
                    if os.path.exists(path):
                        flag = True
                    elif (int(time.time()) - msg["timestamp"]) > self.time_out:
                        msg_type = msg["type"]
                        msg['message'] = f"[{msg_type} 下载超时,请在手机端查看]"
                        msg["type"] = "text"
                        flag = True
                    elif msg["type"] == "voice":
                        sql = f'SELECT Buf FROM Media WHERE Reserved0 = {msg["msgid"]}'
                        dbresult = self.bot.QueryDatabase(db_handle=self.bot.GetDBHandle("MediaMSG0.db"), sql=sql)["data"]
                        if len(dbresult) == 2:
                            filebuffer = dbresult[1][0]
                            decoded = bytes(base64.b64decode(filebuffer))
                            with open(msg["filepath"], 'wb') as f:
                                f.write(decoded)
                            f.close()
                            flag = True

                    if flag:
                        del self.file_msg[path]
                        self.send_efb_msgs(MsgWrapper(msg, MsgProcess(msg, chat)), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))

            if len(self.delete_file):
                for k in list(self.delete_file.keys()):
                    file_path = k
                    begin_time = self.delete_file[k]
                    if  (int(time.time()) - begin_time) > self.time_out:
                        try:
                            os.remove(file_path)
                        except:
                            pass
                        del self.delete_file[file_path]

    def process_friend_request(self , v3 , v4):
        self.logger.debug(f"process_friend_request:{v3} {v4}")
        res = self.bot.VerifyApply(v3 = v3 , v4 = v4)
        if str(res['msg']) != "0":
            return "Success"
        else:
            return "Failed"

    def process_transfer(self, transcationid , transferid , wxid):
        res = self.bot.GetTransfer(transcationid = transcationid , transferid = transferid , wxid = wxid)
        if str(res["msg"]) != "0":
            return "Success"
        else:
            return "Failed"

    def add_friend(self , v3):
        res = self.bot.AddContactByV3(v3 = v3 , msg = "")
        if str(res['msg']) != "0":
            return "Success"
        else:
            return "Failed"

    # 定时任务
    def scheduled_job(self):
        count = 1
        while True:
            time.sleep(1)
            if count % 1800 == 1:
                self.GetGroupListBySql()
                self.GetContactListBySql()
                count = 1
            else:
                count += 1

    #获取全部联系人
    def get_chats(self) -> Collection['Chat']:
        if not self.friends and not self.groups:
            self.GetContactListBySql()
        return self.groups + self.friends

    #获取联系人
    def get_chat(self, chat_uid: ChatID) -> 'Chat':
        if not self.friends and not self.groups:
            self.GetContactListBySql()

        if "@chatroom" in chat_uid:
            for group in self.groups:
                if group.uid == chat_uid:
                    return group
        else:
            for friend in self.friends:
                if friend.uid == chat_uid:
                    return friend
        raise EFBChatNotFound

    #发送消息
    def send_message(self, msg : Message) -> Message:
        chat_uid = msg.chat.uid

        if msg.edit:
            pass     # todo

        if msg.text:
            match = re.search(self.forward_pattern, msg.text)
            if match:
                if match.group(1) == hashlib.md5(self.channel_id.encode('utf-8')).hexdigest():
                    msgid = match.group(2)
                    self.logger.debug(f"提取到的消息 ID: {msgid}")
                    self.bot.ForwardMessage(wxid = chat_uid, msgid = msgid)
                else:
                    self.logger.debug(f"非本 slave 消息: {match.group(1)}/{match.group(2)}")
                return msg

        if msg.type == MsgType.Voice:
            f = tempfile.NamedTemporaryFile(prefix='voice_message_', suffix=".mp3")
            AudioSegment.from_ogg(msg.file.name).export(f, format="mp3")
            msg.file = f
            msg.file.name = "语音留言.mp3"
            msg.type = MsgType.Video
            msg.filename = os.path.basename(f.name)

        if msg.type in [MsgType.Text]:
            if msg.text.startswith('/changename'):
                newname = msg.text.strip('/changename ')
                res = self.bot.SetChatroomName(chatroom_id = chat_uid , chatroom_name = newname)
            elif msg.text.startswith('/getmemberlist'):
                memberlist = self.bot.GetChatroomMemberList(chatroom_id = chat_uid)
                message = '群组成员包括：'
                for wxid in memberlist['members'].split('^G'):
                    try:
                        name = self.contacts[wxid]
                    except:
                        try:
                            name = self.bot.GetChatroomMemberNickname(chatroom_id = chat_uid, wxid = wxid)['nickname'] or wxid
                        except:
                            name = wxid
                    message += '\n' + wxid + ' : ' + name
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/getstaticinfo'):
                info = msg.text[15::]
                if info == 'friends':
                    message = str(self.friends)
                elif info == 'groups':
                    message = str(self.groups)
                elif info == 'group_members':
                    message = json.dumps(self.group_members)
                elif info == 'contacts':
                    message = json.dumps(self.contacts)
                else:
                    message = '当前仅支持查询friends, groups, group_members, contacts'
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/helpcomwechat'):
                message = '''/search - 按关键字匹配好友昵称搜索联系人

/addtogroup - 按wxid添加好友到群组

/getmemberlist - 查看群组用户wxid

/at - 后面跟wxid，多个用英文,隔开，最后可用空格隔开，带内容。

/sendcard - 后面格式'wxid nickname'

/changename - 修改群组名称

/addfriend - 后面格式'wxid message'

/getstaticinfo - 可获取friends, groups, contacts信息'''
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/search'):
                keyword = msg.text[8::]
                message = 'result:'
                for key, value in self.contacts.items():
                    if keyword in value:
                        message += '\n' + str(key) + " : " + str(value)
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/addtogroup'):
                users = msg.text[12::]
                res = self.bot.AddChatroomMember(chatroom_id = chat_uid, wxids = users)
            elif msg.text.startswith('/forward'):
                if isinstance(msg.target, Message):
                    msgid = msg.target.uid
                    if msgid.isdecimal():
                        url = f"ehforwarderbot://{hashlib.md5(self.channel_id.encode('utf-8')).hexdigest()}/forward/{msgid}"
                        prompt = "请将这条信息转发到目标聊天中"
                        text = f"{url}\n{prompt}"
                        if msg.target.text:
                            match = re.search(self.forward_pattern, msg.target.text)
                            if match:
                                msg.target.text = f"{msg.target.text[0:match.start()]}{text}"
                            else:
                                msg.target.text = f"{msg.target.text}\n\n---\n{text}"
                        else:
                            msg.target.text = text
                        self.send_efb_msgs(msg.target, edit=True)
                    else:
                        text = f"无法转发{msgid},不是有效的微信消息"
                        self.system_msg({'sender': chat_uid, 'message': text, 'target': msg.target})
                    return msg
            elif msg.text.startswith('/at'):
                users_message = msg.text[4::].split(' ', 1)
                if isinstance(msg.target, Message):
                    users = msg.target.author.uid
                    message = msg.text[4::]
                elif len(users_message) == 2:
                    users, message = users_message
                else:
                    users, message = users_message[0], ''
                if users != '':
                    res = self.bot.SendAt(chatroom_id = chat_uid, wxids = users, msg = message)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            elif msg.text.startswith('/sendcard'):
                user_nickname = msg.text[10::].split(' ', 1)
                if len(user_nickname) == 2:
                    user, nickname = user_nickname
                else:
                    user, nickname = user_nickname[0], ''
                if user != '':
                    res = self.bot.SendCard(receiver = chat_uid, share_wxid = user, nickname = nickname)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            elif msg.text.startswith('/addfriend'):
                user_invite = msg.text[11::].split(' ', 1)
                if len(user_invite) == 2:
                    user, invite = user_invite
                else:
                    user, invite = user_invite[0], ''
                if user != '':
                    res = self.bot.AddContactByWxid(wxid = user, msg = invite)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            else:
                res = self.send_text(wxid = chat_uid , msg = msg)
        elif msg.type in [MsgType.Link]:
            self.send_text(wxid = chat_uid , msg = msg)
        elif msg.type in [MsgType.Image , MsgType.Sticker]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                img_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {img_path}")
            else:
                img_path = os.path.join(self.base_path, self.wxid, name)
            
            self.logger.debug(f"发送图片路径: {img_path}")
            res = self.bot.SendImage(receiver = chat_uid , img_path = img_path)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)
        elif msg.type in [MsgType.File , MsgType.Video]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            if msg.filename:
                try:
                    os.rename(local_path , f"{self.dir}{self.wxid}/{msg.filename}")
                except:
                    os.replace(local_path , f"{self.dir}{self.wxid}/{msg.filename}")
                local_path = f"{self.dir}{self.wxid}/{msg.filename}"
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                file_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {file_path}")
            else:
                filename = msg.filename if msg.filename else name
                file_path = os.path.join(self.base_path, self.wxid, filename)
            
            self.logger.debug(f"发送文件路径: {file_path}")
            res = self.bot.SendFile(receiver = chat_uid , file_path = file_path)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)
            if msg.type == MsgType.Video:
                res["msg"] = 1
        elif msg.type in [MsgType.Animation]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                file_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {file_path}")
            else:
                file_path = os.path.join(self.base_path, self.wxid, name)
            
            self.logger.debug(f"发送动画表情路径: {file_path}")
            res = self.bot.SendEmotion(wxid = chat_uid , img_path = file_path)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)

        try:
            if str(res["msg"]) == "0":
                raise EFBMessageError("发送失败，请在手机端确认")
        except:
            ...
        return msg

    def send_text(self, wxid: ChatID, msg: Message) -> 'Message':
        text = msg.text
        if isinstance(msg.target, Message):
                if isinstance(msg.target.author, SelfChatMember) and isinstance(msg.target.deliver_to, SlaveChannel):
                    qt_txt = msg.target.text or msg.target.type.name
                    text = qutoed_text(qt_txt, msg.text)
                else:
                    msgid = msg.target.uid
                    sender = msg.target.author.uid
                    displayname = msg.target.author.name
                    content = escape(msg.target.vendor_specific.get("wx_xml", ""), {
                        "\n": "&#x0A;",
                        "\t": "&#x09;",
                        '"': "&quot;",
                    }) or msg.target.text
                    comwechat_info = msg.target.vendor_specific.get("comwechat_info", {})
                    if comwechat_info.get("type", None) == "animatedsticker":
                        refer_type = 47
                    elif msg.target.type == MsgType.Image:
                        refer_type = 3
                    elif msg.target.type == MsgType.Voice:
                        refer_type = 34
                    elif msg.target.type == MsgType.Video:
                        refer_type = 43
                    elif msg.target.type == MsgType.Sticker:
                        refer_type = 47
                    elif msg.target.type == MsgType.Location:
                        refer_type = 48
                    elif msg.target.type == MsgType.File:
                        refer_type = 49
                    elif comwechat_info.get("type", None) == "share":
                        refer_type = 49
                    else:
                        refer_type = 1
                    if content:
                        content = "<content>%s</content>" % content
                    else:
                        content = "<content />"
                    xml = QUOTE_MESSAGE % (self.wxid, text, refer_type, msgid, sender, sender, displayname, content)
                    return self.bot.SendXml(wxid = wxid , xml = xml, img_path = "")
        return self.bot.SendText(wxid = wxid , msg = text)

    def get_chat_picture(self, chat: 'Chat') -> BinaryIO:
        wxid = chat.uid
        result = self.bot.GetPictureBySql(wxid = wxid)
        if result:
            return download_file(result)
        else:
            return None

    def get_chat_member_picture(self, chat_member: 'ChatMember') -> BinaryIO:
        wxid = chat_member.uid
        result = self.bot.GetPictureBySql(wxid = wxid)
        if result:
            return download_file(result)
        else:
            return None

    def poll(self):
        timer = threading.Thread(target = self.scheduled_job)
        timer.daemon = True
        timer.start()

        self.bot.run(main_thread = False)

        t = threading.Thread(target = self.handle_file_msg)
        t.daemon = True
        t.start()

    def send_status(self, status: 'Status'):
        ...

    def stop_polling(self):
        ...

    def get_message_by_id(self, chat: 'Chat', msg_id: MessageID) -> Optional['Message']:
        ...

    def get_name_by_wxid(self, wxid):
        try:
            name = self.contacts[wxid]
            if name == "":
                name = wxid
        except:
            data = self.bot.GetContactBySql(wxid = wxid)
            if data:
                name = data[3]
                if name == "":
                    name = wxid
            else:
                name = wxid
        return name

    #定时更新 Start
    def GetContactListBySql(self):
        self.groups = []
        self.friends = []
        contacts = self.bot.GetContactListBySql()
        for contact in contacts:
            data = contacts[contact]
            name = (f"{data['remark']}({data['nickname']})") if data["remark"] else data["nickname"]

            self.contacts[contact] = name
            if data["type"] == 0 or data["type"] == 4:
                continue

            if "@chatroom" in contact:
                new_entity = EFBGroupChat(
                    uid=contact,
                    name=name
                )
                self.groups.append(ChatMgr.build_efb_chat_as_group(new_entity))
            else:
                new_entity = EFBPrivateChat(
                    uid=contact,
                    name=name
                )
                self.friends.append(ChatMgr.build_efb_chat_as_private(new_entity))

    def GetGroupListBySql(self):
        self.group_members = self.bot.GetAllGroupMembersBySql()
    #定时更新 End
    
    def _detect_wsl(self) -> bool:
        """检测是否在WSL环境中运行"""
        try:
            # 检查/proc/version文件是否包含WSL标识
            if os.path.exists('/proc/version'):
                with open('/proc/version', 'r') as f:
                    version_info = f.read().lower()
                    return 'microsoft' in version_info or 'wsl' in version_info
            return False
        except:
            return False
    
    def _wsl_to_windows_path(self, wsl_path: str) -> str:
        """将WSL路径转换为Windows路径"""
        if not self.is_wsl:
            return wsl_path
            
        try:
            # 处理 /mnt/c/ 格式的路径
            if wsl_path.startswith('/mnt/'):
                # /mnt/c/Users/... -> C:\Users\...
                parts = wsl_path.split('/', 3)
                if len(parts) >= 3:
                    drive_letter = parts[2].upper()
                    if len(parts) > 3:
                        path_part = parts[3].replace('/', '\\')
                        return f"{drive_letter}:\\{path_part}"
                    else:
                        return f"{drive_letter}:\\"
            
            # 如果不是/mnt/格式，尝试使用wslpath命令转换
            import subprocess
            result = subprocess.run(['wslpath', '-w', wsl_path], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            self.logger.warning(f"WSL路径转换失败: {wsl_path}, 错误: {e}")
        
        # 转换失败时返回原路径
        return wsl_path


