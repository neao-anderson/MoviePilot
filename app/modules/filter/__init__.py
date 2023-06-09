import re
from typing import List, Tuple, Union, Dict, Optional

from app.core import TorrentInfo, settings
from app.modules import _ModuleBase
from app.modules.filter.RuleParser import RuleParser


class FilterModule(_ModuleBase):

    # 规则解析器
    parser: RuleParser = None

    # 内置规则集
    rule_set: Dict[str, dict] = {
        # 蓝光
        "BLU": {
            "include": [r'Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC'],
            "exclude": []
        },
        # 4K
        "4K": {
            "include": [r'4k|2160p|x2160'],
            "exclude": []
        },
        # 1080P
        "1080P": {
            "include": [r'1080[pi]|x1080'],
            "exclude": []
        },
        # 中字
        "CN": {
            "include": [r'特效|[中国國繁简](/|\s|\\|\|)?[繁简英粤]|[英简繁](/|\s|\\|\|)?[中繁简]|繁體|简体|[中国國][字配]|国语|國語|中文'],
            "exclude": []
        },
        # H265
        "H265": {
            "include": [r'[Hx].?265'],
            "exclude": []
        },
        # H264
        "H264": {
            "include": [r'[Hx].?264'],
            "exclude": []
        },
        # 杜比
        "DOLBY": {
            "include": [r"DOLBY|DOVI|\s+DV$|\s+DV\s+"],
            "exclude": []
        },
        # HDR
        "HDR": {
            "include": [r"\s+HDR\s+|HDR10|HDR10\+"],
            "exclude": []
        },
        # 重编码
        "REMUX": {
            "include": [r'REMUX'],
            "exclude": []
        }
    }

    def init_module(self) -> None:
        self.parser = RuleParser()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "FILTER_RULE", True

    def filter_torrents(self, torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param torrent_list:  资源列表
        :return: 过滤后的资源列表，添加资源优先级
        """
        # 返回种子列表
        ret_torrents = []
        for torrent in torrent_list:
            # 能命中优先级的才返回
            if self.__get_order(torrent, settings.FILTER_RULE):
                ret_torrents.append(torrent)

        return ret_torrents

    def __get_order(self, torrent: TorrentInfo, rule_str: str) -> Optional[TorrentInfo]:
        """
        获取种子匹配的规则优先级，值越大越优先，未匹配时返回None
        """
        # 多级规则
        rule_groups = rule_str.split('>')
        # 优先级
        res_order = 100
        # 是否匹配
        matched = False

        for rule_group in rule_groups:
            # 解析规则组
            parsed_group = self.parser.parse(rule_group.strip())
            if self.__match_group(torrent, parsed_group.as_list()[0]):
                # 出现匹配时中断
                matched = True
                torrent.pri_order = res_order
                break
            # 优先级降低，继续匹配
            res_order -= 1

        return None if not matched else torrent

    def __match_group(self, torrent: TorrentInfo, rule_group: Union[list, str]) -> bool:
        """
        判断种子是否匹配规则组
        """
        if not isinstance(rule_group, list):
            # 不是列表，说明是规则名称
            return self.__match_rule(torrent, rule_group)
        elif isinstance(rule_group, list) and len(rule_group) == 1:
            # 只有一个规则项
            return self.__match_rule(torrent, rule_group[0])
        elif rule_group[0] == "not":
            # 非操作
            return not self.__match_group(torrent, rule_group[1:])
        elif rule_group[1] == "and":
            # 与操作
            return self.__match_group(torrent, rule_group[0]) and self.__match_group(torrent, rule_group[2:])
        elif rule_group[1] == "or":
            # 或操作
            return self.__match_group(torrent, rule_group[0]) or self.__match_group(torrent, rule_group[2:])

    def __match_rule(self, torrent: TorrentInfo, rule_name: str) -> bool:
        """
        判断种子是否匹配规则项
        """
        if not self.rule_set.get(rule_name):
            # 规则不存在
            return False
        # 包含规则项
        includes = self.rule_set[rule_name].get("include")
        # 排除规则项
        excludes = self.rule_set[rule_name].get("exclude")
        # 匹配项
        content = f"{torrent.title} {torrent.description}"
        for include in includes:
            if not re.search(r"%s" % include, content, re.IGNORECASE):
                # 未发现包含项
                return False
        for exclude in excludes:
            if re.search(r"%s" % exclude, content, re.IGNORECASE):
                # 发现排除项
                return False
        return True
