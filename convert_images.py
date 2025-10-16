import re
import base64
import os
import sys

def process_markdown_file(md_file_path):
    """
    处理单个 Markdown 文件，转换 Base64 图片为文件链接。
    """
    
    # 假设脚本在项目根目录运行，定义图片保存目录和网页访问路径
    # 例如，如果 md_file_path 是 'source_posts/my-post.md'
    # 项目根目录就是 ' ' (当前目录)
    # 图片保存的物理路径是 'source/img/blobs/'
    # 图片在网页上的访问路径是 '/img/blobs/'
    
    img_save_dir = os.path.join('source', 'img', 'blobs')
    web_path_prefix = '/img/blobs/'

    # 确保图片保存目录存在
    os.makedirs(img_save_dir, exist_ok=True)

    print(f"📄 正在处理文件: {md_file_path}")

    # 读取 Markdown 文件内容
    try:
        with open(md_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"❌ 错误: 文件未找到 {md_file_path}")
        return

    # 正则表达式，用于查找文件末尾的 Base64 引用
    # 格式: [1749463049348]:data:image/png;base64,iVBORw0...
    # 分组 1: 图片ID (例如 1749463049348)
    # 分组 2: Base64 数据
    base64_ref_pattern = re.compile(
        r'^\[(\d+)\]:\s*data:image/png;base64,([A-Za-z0-9+/=\s]+)$',
        re.MULTILINE
    )

    # 创建一个待办事项列表，避免在迭代时修改字符串
    matches = list(base64_ref_pattern.finditer(content))
    
    if not matches:
        print("🤷‍ 没有找到符合格式的 Base64 图片引用。")
        return

    modified_content = content
    images_converted = 0

    for match in matches:
        image_id = match.group(1)
        base64_data = match.group(2).strip() # 去除可能存在的换行符
        full_ref_line = match.group(0)

        print(f"  🖼️ 发现图片 ID: {image_id}")

        # 1. 解码 Base64 并保存为图片文件
        try:
            image_data = base64.b64decode(base64_data)
            image_filename = f"{image_id}.png"
            image_save_path = os.path.join(img_save_dir, image_filename)

            with open(image_save_path, 'wb') as img_file:
                img_file.write(image_data)
            
            print(f"    ✅ 已保存图片到: {image_save_path}")

        except (base64.binascii.Error, ValueError) as e:
            print(f"    ❌ 错误: 解码 ID {image_id} 的 Base64 数据失败，已跳过。错误信息: {e}")
            continue

        # 2. 准备新的 Markdown 图片链接
        new_image_web_path = f"{web_path_prefix}{image_filename}"

        # 3. 在 Markdown 内容中查找并替换图片标签 ![...][id]
        # 使用 re.escape 来确保 image_id 中的数字被正确匹配
        tag_pattern = re.compile(r'!\[(.*?)\]\[' + re.escape(image_id) + r'\]')
        
        # 使用一个替换函数来保留原始的 Alt Text
        def tag_replacer(m):
            alt_text = m.group(1)
            return f'![{alt_text}]({new_image_web_path})'

        # 执行替换
        modified_content, num_replacements = tag_pattern.subn(tag_replacer, modified_content)

        if num_replacements > 0:
            print(f"    ✅ 已将 {num_replacements} 处图片标签更新为链接: {new_image_web_path}")
            images_converted += num_replacements
        else:
            print(f"    ⚠️ 警告: 找到了 Base64 数据，但未在文中找到对应的 ![...][{image_id}] 标签。")

        # 4. 从内容中移除原始的 Base64 引用行
        modified_content = modified_content.replace(full_ref_line, '')

    # 5. 清理可能因删除引用行而产生的多余空行
    modified_content = re.sub(r'\n\s*\n', '\n', modified_content).strip()

    # 6. 将修改后的内容写回原文件
    if images_converted > 0:
        with open(md_file_path, 'w', encoding='utf-8') as f:
            f.write(modified_content)
        print(f"\n✨ 处理完成！总共转换了 {images_converted} 张图片，并已更新文件 {md_file_path}。")
    else:
        print("\n✨ 处理完成，但没有进行任何替换。")


if __name__ == '__main__':
    # 从命令行参数获取要处理的 markdown 文件路径
    if len(sys.argv) < 2:
        print("用法: python convert_images.py <markdown文件路径>")
        print("例如: python convert_images.py source_posts/my-article.md")
        sys.exit(1)
    
    markdown_file = sys.argv[1]
    process_markdown_file(markdown_file)

