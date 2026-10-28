"""Small compatibility record; API availability is not hardware validation."""
import re


VERIFIED_FIRMWARE = {('HERO9', 'HD9.01.01.72.00')}
WIRELESS_DELETE_FIRMWARE = {('HERO9', 'HD9.01.01.72.00')}


def model_key(name):
    return re.sub(r'\s+Black$', '', str(name), flags=re.I).strip().upper()


def compatibility(name, firmware='', transport='usb'):
    model = model_key(name)
    supported = model in {'HERO9', 'HERO10', 'HERO11', 'HERO11 BLACK MINI', 'HERO11 MINI', 'HERO12', 'HERO13'}
    verified = (model, firmware) in VERIFIED_FIRMWARE
    return {'model': model, 'import_supported': supported,
            'validation': '实机已验证' if verified else '待实机验证' if supported else '尚未适配',
            'delete_verified': verified if transport == 'usb' else (model, firmware) in WIRELESS_DELETE_FIRMWARE if transport == 'wifi' else False,
            'delete_transport': transport,
            'chapter_metadata_fallback': model == 'HERO9',
            'mp4_identity_metadata': model == 'HERO9',
            'reason': ('视频、照片导入与归档已验证；支持 MP4、关联 LRV 和普通单张 JPG 清理，连拍与 RAW 保留。' if verified else
                       '可确认后尝试导入；此型号或固件尚未完成实机验收，自动删除不可用。' if supported else
                       '仅识别设备信息，暂不开放导入或删除。')}
