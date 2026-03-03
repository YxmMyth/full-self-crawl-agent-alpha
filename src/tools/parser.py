"""
HTML 解析工具 - 封装 BeautifulSoup
"""

from typing import Dict, Any, List, Optional, Union
from bs4 import BeautifulSoup, Tag, NavigableString
from urllib.parse import urljoin, urlparse
import re


class HTMLParser:
    """
    HTML 解析工具

    基于 BeautifulSoup 的 HTML 解析器
    提供便捷的数据提取方法
    """

    def __init__(self, html: str, base_url: str = ''):
        self.soup = BeautifulSoup(html, 'html.parser')
        self.base_url = base_url

    def set_base_url(self, url: str) -> None:
        """设置基准 URL（用于相对链接解析）"""
        self.base_url = url

    def select(self, selector: str) -> List[Tag]:
        """使用 CSS 选择器选择元素"""
        return self.soup.select(selector)

    def select_one(self, selector: str) -> Optional[Tag]:
        """选择单个元素"""
        return self.soup.select_one(selector)

    def find_by_id(self, elem_id: str) -> Optional[Tag]:
        """通过 ID 查找元素"""
        return self.soup.find(id=elem_id)

    def find_by_class(self, class_name: str) -> List[Tag]:
        """通过类名查找元素"""
        return self.soup.find_all(class_=class_name)

    def find_by_tag(self, tag: str) -> List[Tag]:
        """通过标签名查找元素"""
        return self.soup.find_all(tag)

    def get_text(self, element: Optional[Tag] = None, strip: bool = True) -> str:
        """获取元素的文本内容"""
        if element:
            return element.get_text(strip=strip)
        return self.soup.get_text(strip=strip)

    def get_attribute(self, element: Tag, attr: str) -> Optional[str]:
        """获取元素属性"""
        return element.get(attr)

    def get_href(self, element: Tag) -> Optional[str]:
        """获取链接地址（自动补全为绝对 URL）"""
        href = element.get('href')
        if href:
            return urljoin(self.base_url, href)
        return None

    def get_src(self, element: Tag) -> Optional[str]:
        """获取资源地址（自动补全为绝对 URL）"""
        src = element.get('src')
        if src:
            return urljoin(self.base_url, src)
        return None

    def extract_table(self, table_selector: str,
                     has_header: bool = True) -> List[Dict[str, str]]:
        """提取表格数据"""
        table = self.select_one(table_selector)
        if not table:
            return []

        headers = []
        if has_header:
            header_row = table.select_one('thead tr')
            if header_row:
                headers = [th.get_text(strip=True) for th in header_row.select('th')]
            else:
                # 第一行作为表头
                first_row = table.select_one('tr')
                if first_row:
                    headers = [td.get_text(strip=True) for td in first_row.select('td')]

        rows = []
        for row in table.select('tbody tr'):
            cells = [td.get_text(strip=True) for td in row.select('td')]
            if headers:
                row_data = dict(zip(headers, cells))
            else:
                row_data = {f'col_{i}': cell for i, cell in enumerate(cells)}
            rows.append(row_data)

        return rows

    def extract_links(self, selector: str = 'a',
                     text_only: bool = False) -> List[Dict[str, str]]:
        """提取链接"""
        links = []
        for link in self.select(selector):
            href = link.get('href')
            text = link.get_text(strip=True)

            if not href:
                continue

            full_url = urljoin(self.base_url, href)

            if text_only:
                links.append(text)
            else:
                links.append({
                    'url': full_url,
                    'text': text
                })

        return links

    def extract_images(self, selector: str = 'img') -> List[Dict[str, str]]:
        """提取图片"""
        images = []
        for img in self.select(selector):
            src = img.get('src')
            alt = img.get('alt', '')

            if not src:
                continue

            full_url = urljoin(self.base_url, src)
            images.append({
                'url': full_url,
                'alt': alt
            })

        return images

    def extract_list(self, selector: str) -> List[str]:
        """提取列表项文本"""
        items = []
        for item in self.select(selector):
            text = item.get_text(strip=True)
            if text:
                items.append(text)
        return items

    def extract_by_xpath(self, xpath: str) -> List[Any]:
        """
        通过 XPath 提取元素

        需要安装 lxml
        """
        try:
            from lxml import etree
            dom = etree.HTML(str(self.soup))
            return dom.xpath(xpath)
        except ImportError:
            print('lxml not installed, xpath not available')
            return []

    def to_dict(self, element: Optional[Tag] = None) -> Dict[str, Any]:
        """
        将元素转换为字典

        包含标签、属性、文本等信息
        """
        if element is None:
            element = self.soup

        return {
            'tag': element.name if element else 'root',
            'attributes': element.attrs if element else {},
            'text': element.get_text(strip=True) if element else '',
            'children': len(element.children) if element else 0
        }

    def get_page_structure(self) -> Dict[str, Any]:
        """分析页面结构"""
        return {
            'title': self.soup.title.string if self.soup.title else '',
            'has_h1': len(self.soup.find_all('h1')) > 0,
            'has_h2': len(self.soup.find_all('h2')) > 0,
            'has_table': len(self.soup.find_all('table')) > 0,
            'has_list': len(self.soup.find_all(['ul', 'ol'])) > 0,
            'link_count': len(self.soup.find_all('a')),
            'image_count': len(self.soup.find_all('img')),
            'form_count': len(self.soup.find_all('form')),
            'script_count': len(self.soup.find_all('script'))
        }

    def detect_pagination(self) -> Dict[str, Any]:
        """检测分页"""
        pagination = {
            'has_next': False,
            'has_prev': False,
            'next_url': None,
            'prev_url': None,
            'page_numbers': []
        }

        # 检测常见的"下一页"链接
        next_selectors = [
            'a.next',
            'a[rel="next"]',
            'a:contains("下一页")',
            'a:contains("Next")',
            'li.next a',
            '.pagination .next a'
        ]

        for selector in next_selectors:
            next_link = self.select_one(selector)
            if next_link:
                pagination['has_next'] = True
                pagination['next_url'] = self.get_href(next_link)
                break

        # 检测页码
        page_patterns = [
            r'\?page=\d+',
            r'/page/\d+',
            r'p=\d+'
        ]

        for link in self.select('a[href]'):
            href = link.get('href', '')
            for pattern in page_patterns:
                if re.search(pattern, href):
                    match = re.search(r'\d+', href)
                    if match:
                        pagination['page_numbers'].append(int(match.group()))

        if pagination['page_numbers']:
            pagination['page_numbers'] = sorted(set(pagination['page_numbers']))

        return pagination


class SelectorBuilder:
    """
    选择器构建器

    帮助构建复杂的 CSS 选择器
    """

    @staticmethod
    def class_name(name: str) -> str:
        """构建类选择器"""
        return f'.{name}'

    @staticmethod
    def id_name(name: str) -> str:
        """构建 ID 选择器"""
        return f'#{name}'

    @staticmethod
    def attribute(name: str, value: Optional[str] = None) -> str:
        """构建属性选择器"""
        if value:
            return f'[{name}="{value}"]'
        return f'[{name}]'

    @staticmethod
    def contains(tag: str, text: str) -> str:
        """构建包含文本的选择器"""
        return f'{tag}:contains("{text}")'

    @staticmethod
    def nth_child(tag: str, n: int) -> str:
        """构建第 N 个子元素选择器"""
        return f'{tag}:nth-child({n})'

    @staticmethod
    def combine(*selectors: str) -> str:
        """组合多个选择器"""
        return ' '.join(selectors)

    @staticmethod
    def child(parent: str, child: str) -> str:
        """构建父子选择器"""
        return f'{parent} > {child}'

    @staticmethod
    def descendant(ancestor: str, descendant: str) -> str:
        """构建后代选择器"""
        return f'{ancestor} {descendant}'
