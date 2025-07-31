"""
CABM应用主文件
"""
import os
import sys
import json
from io import BytesIO
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, Response ,send_file
from werkzeug.utils import secure_filename
from pydub import AudioSegment
# 添加项目根目录到系统路径
sys.path.append(str(Path(__file__).resolve().parent))

from services.config_service import config_service
from services.chat_service import chat_service
from services.image_service import image_service
from services.scene_service import scene_service
from services.option_service import option_service
from services.gsapi_service import ttsService
from services.damoasr_service import *
from utils.api_utils import APIError

# 初始化配置
if not config_service.initialize():
    print("配置初始化失败")
    sys.exit(1)

# 获取应用配置
app_config = config_service.get_app_config()

# 创建Flask应用
app = Flask(
    __name__,
    static_folder=app_config["static_folder"],
    template_folder=app_config["template_folder"]
)

# 设置调试模式
app.debug = app_config["debug"]

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def convert_to_16k_wav(input_path, output_path):
    """转换音频为 16kHz 单声道 WAV"""
    audio = AudioSegment.from_file(input_path)
    audio_16k = audio.set_frame_rate(16000).set_channels(1)
    audio_16k.export(output_path, format="wav")
    return output_path

@app.route('/')
def index():
    """首页"""
    # 获取当前背景图片
    background = image_service.get_current_background()
    
    # 如果没有背景图片，生成一个
    if not background:
        try:
            result = image_service.generate_background()
            if "image_path" in result:
                background = result["image_path"]
        except Exception as e:
            print(f"背景图片生成失败: {str(e)}")
    
    # 将背景路径转换为URL
    background_url = None
    if background:
        # 从绝对路径转换为相对URL
        rel_path = os.path.relpath(background, start=app.static_folder)
        background_url = f"/static/{rel_path.replace(os.sep, '/')}"
    
    # 检查默认角色图片是否存在，如果不存在则创建一个提示
    character_image_path = os.path.join(app.static_folder, 'images', 'default', '1.png')
    if not os.path.exists(character_image_path):
        print(f"警告: 默认角色图片不存在: {character_image_path}")
        print("请将角色图片放置在 static/images/default/1.png")
    
    # 获取当前场景
    current_scene = scene_service.get_current_scene()
    scene_data = current_scene.to_dict() if current_scene else None
    
    # 获取应用配置
    app_config = config_service.get_app_config()
    show_scene_name = app_config.get("show_scene_name", True)
    
    # 渲染模板
    return render_template(
        'index.html',
        background_url=background_url,
        current_scene=scene_data,
        show_scene_name=show_scene_name
    )

@app.route('/api/mic', methods=['POST'])
def mic_transcribe():
    if 'audio' not in request.files:
        return jsonify({'error': '缺少音频文件'}), 400

    file = request.files['audio']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    filename = secure_filename(file.filename)
    temp_input = os.path.join(UPLOAD_FOLDER, filename)
    wav_path = os.path.join(UPLOAD_FOLDER, "temp_recording.wav")

    try:
        # 保存上传文件
        file.save(temp_input)

        # 转换格式
        convert_to_16k_wav(temp_input, wav_path)

        # 调用 ASR 服务识别
        text = transcribe_audio(wav_path)

        return jsonify({'text': text})

    except Exception as e:
        return jsonify({'error': '语音识别失败', 'detail': str(e)}), 500

    finally:
        # 清理临时文件
        if os.path.exists(temp_input):
            os.remove(temp_input)
        if os.path.exists(wav_path):
            os.remove(wav_path)
@app.route('/api/chat', methods=['POST'])
def chat():
    """聊天API"""
    try:
        # 获取请求数据
        data = request.json
        message = data.get('message', '')
        
        if not message:
            return jsonify({
                'success': False,
                'error': '消息不能为空'
            }), 400
        
        # 添加用户消息
        chat_service.add_message("user", message)
        
        # 调用对话API（传递用户查询用于记忆检索）
        response = chat_service.chat_completion(stream=False, user_query=message)
        
        # 获取助手回复
        assistant_message = None
        if "choices" in response and len(response["choices"]) > 0:
            message_data = response["choices"][0].get("message", {})
            if message_data and "content" in message_data:
                assistant_message = message_data["content"]
        
        # 返回响应
        return jsonify({
            'success': True,
            'message': assistant_message,
            'history': [msg.to_dict() for msg in chat_service.get_history()]
        })
        
    except APIError as e:
        return jsonify({
            'success': False,
            'error': e.message
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/chat/stream', methods=['POST'])
def chat_stream():
    """流式聊天API - 只负责转发AI响应"""
    try:
        # 获取请求数据
        data = request.json
        message = data.get('message', '')
        
        if not message:
            return jsonify({
                'success': False,
                'error': '消息不能为空'
            }), 400
        
        # 添加用户消息
        chat_service.add_message("user", message)
        
        # 创建流式响应生成器
        def generate():
            try:
                # 调用对话API（流式，传递用户查询用于记忆检索）
                stream_gen = chat_service.chat_completion(stream=True, user_query=message)
                full_content = ""
                
                # 逐步返回响应
                for chunk in stream_gen:
                    # 如果是结束标记
                    # if '[DONE]' in chunk:
                    #     break
                    
                    # 处理增量内容
                    if chunk is not None:
                        content = chunk
                        full_content += content
                        
                        # 直接转发原始数据，让前端处理
                        yield f"data: {json.dumps({'content': content})}\n\n"
                            
                # 将完整消息添加到历史记录
                if full_content:
                    chat_service.add_message("assistant", full_content)
                    # 添加到记忆数据库
                    try:
                        character_id = chat_service.config_service.current_character_id or "default"
                        chat_service.memory_service.add_conversation(
                            user_message=message,
                            assistant_message=full_content,
                            character_name=character_id
                        )
                    except Exception as e:
                        print(f"添加对话到记忆数据库失败: {e}")
                    
                    # 生成选项
                    try:
                        conversation_history = chat_service.format_messages()
                        character_config = chat_service.get_character_config()
                        options = option_service.generate_options(
                            conversation_history=conversation_history,
                            character_config=character_config,
                            user_query=message
                        )
                        
                        if options:
                            # 发送选项数据
                            yield f"data: {json.dumps({'options': options})}\n\n"
                    except Exception as e:
                        print(f"选项生成失败: {e}")
                        
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                error_msg = str(e)
                print(f"流式响应错误: {error_msg}")
                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                yield "data: [DONE]\n\n"
        
        # 设置响应头
        headers = {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'  # 禁用Nginx缓冲
        }
        
        # 返回流式响应
        return Response(generate(), mimetype='text/event-stream', headers=headers)
        
    except APIError as e:
        return jsonify({
            'success': False,
            'error': e.message
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/background', methods=['POST'])
def generate_background():
    """生成背景图片API"""
    try:
        # 获取请求数据
        data = request.json
        prompt = data.get('prompt')
        
        # 生成背景图片
        result = image_service.generate_background(prompt)
        
        # 如果生成成功
        if "image_path" in result:
            # 从绝对路径转换为相对URL
            rel_path = os.path.relpath(result["image_path"], start=app.static_folder)
            background_url = f"/static/{rel_path.replace(os.sep, '/')}"
            
            return jsonify({
                'success': True,
                'background_url': background_url,
                'prompt': result.get('config', {}).get('prompt')
            })
        
        return jsonify({
            'success': False,
            'error': '背景图片生成失败'
        }), 500
        
    except APIError as e:
        return jsonify({
            'success': False,
            'error': e.message
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/clear', methods=['POST'])
def clear_history():
    """清空对话历史API"""
    try:
        # 清空对话历史
        chat_service.clear_history()
        
        # 设置系统提示词
        prompt_type = request.json.get('prompt_type', 'character')
        chat_service.set_system_prompt(prompt_type)
        
        return jsonify({
            'success': True,
            'message': '对话历史已清空'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/characters', methods=['GET'])
def list_characters():
    """列出可用角色API"""
    try:
        # 获取当前角色
        current_character = chat_service.get_character_config()
        
        # 获取所有可用角色
        available_characters = config_service.list_available_characters()
        
        return jsonify({
            'success': True,
            'current_character': current_character,
            'available_characters': available_characters
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/characters/<character_id>', methods=['POST'])
def set_character(character_id):
    """设置角色API"""
    try:
        # 设置角色
        if chat_service.set_character(character_id):
            # 获取角色配置
            character_config = chat_service.get_character_config()
            
            return jsonify({
                'success': True,
                'character': character_config,
                'message': f"角色已切换为 {character_config['name']}"
            })
        
        return jsonify({
            'success': False,
            'error': f"未找到角色: {character_id}"
        }), 404
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/exit', methods=['POST'])
def exit_app():
    """退出应用API"""
    try:
        os._exit(0) 
        return jsonify({
            'success': True,
            'message': '应用开始退出'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
        
@app.route('/api/characters/<character_id>/images', methods=['GET'])
def get_character_images(character_id):
    """获取角色的所有图片API"""
    try:
        # 获取角色配置
        character_config = config_service.get_character_config(character_id)
        if not character_config:
            return jsonify({
                'success': False,
                'error': f"未找到角色: {character_id}"
            }), 404
        
        # 获取角色图片目录
        image_dir = character_config.get('image', '')
        if not image_dir:
            return jsonify({
                'success': False,
                'error': "角色未配置图片目录"
            }), 400
        
        # 构建完整路径
        full_image_dir = os.path.join(app.static_folder.replace('static', ''), image_dir)
        
        # 检查目录是否存在
        if not os.path.exists(full_image_dir):
            return jsonify({
                'success': False,
                'error': "角色图片目录不存在"
            }), 404
        
        # 获取目录下的所有png文件
        image_files = []
        for filename in os.listdir(full_image_dir):
            if filename.lower().endswith('.png'):
                # 提取数字编号
                name_without_ext = os.path.splitext(filename)[0]
                try:
                    number = int(name_without_ext)
                    image_files.append({
                        'number': number,
                        'filename': filename,
                        'url': f"/{image_dir}/{filename}"
                    })
                except ValueError:
                    # 如果文件名不是数字，跳过
                    continue
        
        # 按数字排序
        image_files.sort(key=lambda x: x['number'])
        
        return jsonify({
            'success': True,
            'images': image_files,
            'default_image': f"/{image_dir}/1.png"
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/data/images/<path:filename>')
def serve_character_image(filename):
    """提供角色图片"""
    return send_from_directory('data/images', filename)


@app.route('/api/tts', methods=['POST'])
def serve_tts():
    tts = ttsService()
    if not tts.running():
        return jsonify({"error": "语音合成服务未启用/连接失败"}), 400
    data = request.get_json()
    text = data.get("text", "").strip()
    role = data.get("role", "AI助手")
    print(f"请求TTS: 角色={role}, 文本={text}")
    if not text:
        return jsonify({"error": "文本为空"}), 400

    try:
        audio_bytes = tts.get_tts(text, role)  # 应返回 bytes
        if not audio_bytes:
            return jsonify({"error": "TTS生成失败"}), 500

        audio_io = BytesIO(audio_bytes)
        audio_io.seek(0)

        return send_file(
            audio_io,
            mimetype='audio/wav',
            as_attachment=False,
            download_name=None
        )
    except Exception as e:
        print(f"TTS error: {e}")
        return jsonify({"error": "语音合成失败"}), 500


if __name__ == '__main__':
    # 设置系统提示词，使用角色提示词
    chat_service.set_system_prompt("character")
    
    # 启动应用
    app.run(
        host=app_config["host"],
        port=app_config["port"],
        debug=app_config["debug"],
        use_reloader=app_config["debug"]  # 只在debug模式下启用重载器
    )