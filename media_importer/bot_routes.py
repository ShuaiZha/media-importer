"""Command vocabulary shared by menus, text shortcuts and card navigation.

Queries resolve to panels; starting a business operation is always explicit.
Existing event keys and aliases remain accepted after a menu upgrade.
"""

COMMANDS = {
    'sync.status': ('备份状态', {'panel': 'status'}, ('同步状态', '状态', '进度', '查看状态', '运行状态', '运行总览', '设备与存储', '当前任务', '归档结果')),
    'sync.start': ('开启备份', {'changes': {'backup': {'enabled': True}}}, ('开启同步',)),
    'sync.stop': ('停止备份', {'changes': {'backup': {'enabled': False}}}, ('停止同步', '立即停止', '关闭同步', '关闭备份')),
    'sync.select': ('选择文件', {'selected_import': {'version': 1, 'action': 'browse'}}, ('选择素材',)),
    'camera.overview': ('相机状态', {'panel': 'camera_status'}, ('相机概览',)),
    'camera.connect': ('连接相机', {'capture': {'version': 1, 'action': 'connect'}}, ('立即连接相机', '连接无线相机')),
    'camera.workspace': ('相机页面', {'capture': {'version': 1, 'action': 'panel'}}, ('相机控制', '拍摄控制', '实时预览')),
    'camera.cleanup': ('清理原片', {'camera': {'version': 1, 'action': 'select', 'mode': 'archived'}}, ()),
    'settings.media': ('素材与清理', {'panel': 'media_settings'}, ()),
    'settings.transfer': ('传输与缓存', {'panel': 'transfer_settings'}, ()),
    'settings.camera': ('相机与画面', {'panel': 'camera_settings'}, ('相机与预览',)),
    'settings.destination': ('归档存储', {'panel': 'destination_settings'}, ('归档位置',)),
    'settings.notifications': ('消息通知', {'panel': 'notification_settings'}, ()),
}
MENUS = (
    ('备份', ('sync.status', 'sync.start', 'sync.stop', 'sync.select')),
    ('相机', ('camera.overview', 'camera.connect', 'camera.workspace', 'camera.cleanup')),
    ('设置', ('settings.media', 'settings.transfer', 'settings.camera', 'settings.destination', 'settings.notifications')),
)
MENU_ACTIONS = {key: action for key, (_, action, _) in COMMANDS.items()}
MENU_ACTIONS.update({
    'sync.cleanup': MENU_ACTIONS['camera.cleanup'],
    'camera.preview': MENU_ACTIONS['camera.workspace'],
    'camera.capture': MENU_ACTIONS['camera.workspace'],
    'settings.source': MENU_ACTIONS['settings.camera'],
})
TEXT_ACTIONS = {name: action for label, action, aliases in COMMANDS.values() for name in (label, *aliases)}
TEXT_ACTIONS.update({name: {'panel': panel} for panel, names in {
    'settings': ('设置', '参数设置'),
    'preview_settings': ('预览设置',), 'preview_check': ('预览连接检查',),
    'source_settings': ('相机连接',), 'advanced_settings': ('高级设置',),
    'local_storage': ('本机目录',),
    'cleanup': ('原片处理', '删除原片', '删除原片开关', '清理设置'),
    'notifications': ('自动通知', '通知开关'),
    'previews': ('预览文件',), 'scope': ('素材范围',),
}.items() for name in names})
for name in ('当前文件完成后停止', '完成当前文件后停止'):
    TEXT_ACTIONS[name] = {'changes': {'backup': {'stop_after_current': True}}, 'settings_version': 2}

HELP = '请使用底部「备份」「相机」「设置」菜单。'
