"""Resolve only public Bilibili share redirects, with no login cookies."""
from urllib.parse import urljoin
import requests
from .command_schema import link_target

def resolve(url):
    current=link_target(url)
    for _ in range(3):
        if current['target_type']!='short':return current
        with requests.get(current['url'],timeout=(5,10),allow_redirects=False,stream=True,
                          headers={'User-Agent':'Mozilla/5.0'}) as response:
            if response.status_code not in (301,302,303,307,308):
                raise ValueError('短链暂时无法解析，请复制完整B站视频或直播链接')
            location=response.headers.get('Location','')
        # Validate every hop BEFORE requesting it. Never open arbitrary hosts,
        # account URLs, private addresses, or credentials from a shared URL.
        current=link_target(urljoin(current['url'],location))
    if current['target_type']=='short':raise ValueError('短链跳转过多，请使用完整链接')
    return current
