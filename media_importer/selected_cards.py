"""Feishu selection forms and receipts for a bounded, source-preserving import."""
import json
import math
import re
import time

from .bot_cards import card, markdown, note, paragraph, plain, progress_bar
from .media import category
from .model import Asset, ConfigError
from .notifications import _size, _duration
from .selected_imports import archive_members
from .store import Store

PAGE_SIZE = 12
ACTIVE = {'queued', 'scanning', 'queued_transfer', 'running', 'waiting'}
PRESERVATION = '本次保留全部相机原片，不改变全量同步与自动删除设置。'


def supported(action):
    if not isinstance(action, dict) or type(action.get('version')) is not int or action['version'] != 1:
        return False
    kind = action.get('action')
    if kind == 'browse':
        return set(action) == {'version', 'action'}
    required = {'version', 'action', 'id'}
    if kind in ('result', 'cancel', 'drain', 'resume'):
        allowed = required
    elif kind == 'confirm':
        required |= {'choice_revision'}
        allowed = required
    elif kind in ('details', 'edit'):
        allowed = required | {'page'}
    elif kind == 'choose':
        required |= {'page', 'choice_revision', 'next_page', 'review'}
        allowed = required
    else:
        return False
    if not required <= set(action) <= allowed:
        return False
    if not isinstance(action['id'], str) or not re.fullmatch('[0-9a-f]{32}', action['id']):
        return False
    if any(type(action[k]) is not int or not 0 <= action[k] <= 100000
           for k in ('page', 'next_page', 'choice_revision') if k in action):
        return False
    return 'review' not in action or type(action['review']) is bool


def parse_choices(fields, page):
    """Validate only the visible page; other saved selections stay server-side."""
    if type(page) is not int or not 0 <= page <= 100000:
        raise ConfigError('素材页码无效，请重新打开选择卡片')
    # A completely empty optional form may omit form_value as well as groups.
    # Clearing this page changes only the draft; review still requires a selection.
    if fields is None:
        fields = {}
    if not isinstance(fields, dict) or not set(fields) <= {'groups'}:
        raise ConfigError('选择内容不完整，请重新打开选择卡片')
    # Feishu omits the optional multi-select field when no option is selected.
    # This parser is reached only through an owner-authorized, token-backed action.
    values = fields.get('groups', [])
    if not isinstance(values, list) or len(values) > PAGE_SIZE:
        raise ConfigError('选择内容无效，请重新打开选择卡片')
    if any(not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,6}', value) for value in values):
        raise ConfigError('选择内容无效，请重新打开选择卡片')
    indices = [int(value) for value in values]
    if len(indices) != len(set(indices)) or any(not page * PAGE_SIZE <= index < (page + 1) * PAGE_SIZE
                                               for index in indices):
        raise ConfigError('选择范围已变化，请重新打开选择卡片')
    return sorted(indices)


def button(bot, label, action, **values):
    return bot.button(label, {'selected_import': {'version': 1, 'action': action, **values}})


def _body(value):
    return json.loads(value or '{}') if isinstance(value, str) or value is None else value


def _count(result, name):
    return max(0, int(result.get(name) or 0))


def _display_order(item):
    path = str(item.get('path', ''))
    return path.rsplit('/', 1)[-1].casefold(), path.casefold()


def _members(plan, selected_only=False):
    groups = plan.get('groups', [])
    selected = set(plan.get('selected', []))
    return [item for index, group in enumerate(groups) if not selected_only or index in selected
            for item in sorted(archive_members(group, plan.get('archive_previews', False)), key=_display_order)]


def _group_label(index, group, archive_previews, *, file_selection=False):
    members = archive_members(group, archive_previews)
    if file_selection:
        item = group['members'][0]
        return f'{item["path"].rsplit("/", 1)[-1]} · {_size(item["size"])}'
    previews = set(group.get('preview_ids', []))
    primary = sorted((item for item in members if item.get('identity_key') not in previews), key=_display_order)
    names = [str(item.get('path', '')).rsplit('/', 1)[-1] for item in primary or members]
    title = names[0] if names else '未命名素材'
    kinds = {category(Asset.from_dict(item)) for item in primary}
    if kinds == {'video'}:
        count = f'{len(primary)} 段视频' if len(primary) > 1 else '1 个视频'
    else:
        count = f'{len(primary)} 个文件'
    preview_count = len(members) - len(primary)
    if preview_count:
        count = (count + ' + ' if primary else '') + f'{preview_count} 个预览文件'
    return f'{index + 1}. {title} · {count} · {_size(sum(item.get("size", 0) for item in members))}'


def _chooser(bot, op, plan, page):
    groups = plan.get('groups', [])
    selected = set(plan.get('selected', []))
    pages = max(1, math.ceil(len(groups) / PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    start = page * PAGE_SIZE
    file_selection = plan.get('selection_mode') == 'files'
    unit = '项' if file_selection else '组'
    elements = [markdown(f'**已保存选择 {len(selected)} {unit}** · 共 {len(groups)} {unit}素材'),
                note('选择需要上传的文件，可多选。'
                     if file_selection else '此清单保留原有分组范围；按文件选择请重新打开「选择素材」。')]
    if not groups:
        return card('没有可选择的素材', [paragraph('本次扫描没有发现相机文件。'), note(PRESERVATION),
                    bot.buttons(button(bot, '重新扫描', 'browse'))], shared=True)
    visible = list(enumerate(groups[start:start + PAGE_SIZE], start))
    # JSON 1.0 multi_select_static is form-only. selected_values are strings;
    # the callback sends form_value.groups as an array, never comma-separated text.
    # https://open.feishu.cn/document/feishu-cards/card-components/interactive-components/multi-select-dropdown-menu
    field = {'tag': 'multi_select_static', 'name': 'groups', 'placeholder': plain('选择本页素材（可多选）'),
             'width': 'fill', 'required': False,
             'selected_values': [str(index) for index, _ in visible if index in selected],
             'options': [{'text': plain(_group_label(index, group, plan.get('archive_previews', False),
                                                   file_selection=file_selection)), 'value': str(index)}
                         for index, group in visible]}
    controls = []
    destinations = []
    if page:
        destinations.append(('保存并到上一页', page - 1, False))
    if page + 1 < pages:
        destinations.append(('保存并到下一页', page + 1, False))
    destinations.append(('保存并预览', page, True))
    for index, (label, destination, review) in enumerate(destinations):
        control = button(bot, label, 'choose', id=op['id'], page=page,
                         next_page=destination, choice_revision=op['choice_revision'], review=review)
        control.update(name=f'selected_submit_{index}', action_type='form_submit',
                       type='primary' if review else 'default')
        controls.append(control)
    elements += [{'tag': 'form', 'name': 'selected_import_form', 'elements': [field, *controls]},
                 note('关联预览文件：' + ('随主文件归档。' if plan.get('archive_previews') else '不归档已确认关联的 LRV、THM。')),
                 note(PRESERVATION), bot.buttons(button(bot, '取消选择', 'cancel', id=op['id']))]
    return card(f'选择素材 · {page + 1}/{pages}', elements, shared=True)


def _summary(result):
    elements = [markdown(f'**归档范围 {_count(result, "total_files")} 个文件 · {_size(result.get("total_bytes", 0))}**')]
    if result.get('already_archived'):
        elements.append(paragraph(f'已有归档 {_count(result, "already_archived")} 个；核验通过后不重复传输。'))
    if result.get('excluded_preview_files'):
        elements.append(paragraph(f'不归档关联预览文件 {_count(result, "excluded_preview_files")} 个。'))
    return elements


def _file_list(members):
    return paragraph('\n'.join(str(item.get('path', '')) + ' · ' + _size(item.get('size', 0))
                               for item in members) or '没有待归档文件')


def _progress(result, state):
    total = _count(result, 'total_files')
    total_bytes = _count(result, 'total_bytes')
    elements = []
    for name, label, count_key, bytes_key in (
            ('import', '本地导入', 'imported_files', 'import_bytes'),
            ('upload', '远端归档', 'uploaded_files', 'upload_bytes')):
        done = min(total, _count(result, count_key))
        stage = result.get('stages', {}).get(name, {})
        percent = 100 * _count(result, bytes_key) / total_bytes if total_bytes else 0
        if total and done == total:
            bar, percent = progress_bar(100)
            phase = '已完成'
        else:
            bar, percent = progress_bar(min(percent, 99.9))
            phase = {'paused': '已停止', 'done': '未完成', 'queued_transfer': '等待执行'}.get(state)
            if phase is None:
                phase = {'downloading': '导入中', 'uploading': '上传中', 'publishing': '确认归档',
                         'scanning': '核对清单', 'verifying': '核验中', 'waiting_source': '等待相机',
                         'waiting_destination': '等待存储', 'waiting_cache': '等待缓存盘',
                         'waiting_space': '等待可用空间', 'waiting_retry': '等待重试',
                         'draining': '正在停止'}.get(stage.get('phase'), '等待恢复' if state == 'waiting' else '')
        elements.append(markdown(f'**{label}　{percent:.1f}%**' + (' · ' + phase if phase else '') +
                                 f'\n{bar}　{done} / {total} 个'))
        if state not in ('paused', 'done') and stage.get('current_file'):
            elements.append(paragraph(str(stage['current_file']).rsplit('/', 1)[-1]))
            speed = stage.get('speed_bytes_per_second')
            if speed:
                detail = _size(speed) + '/s'
                if stage.get('eta_seconds') is not None:
                    detail += ' · 本文件预计剩余 ' + _duration(stage['eta_seconds'])
                elements.append(note(detail))
        if state not in ('paused', 'done') and str(stage.get('phase', '')).startswith('waiting'):
            reason = stage.get('issue', {}) or {}
            reason = reason.get('action') or stage.get('message')
            if reason:
                elements.append(paragraph(reason))
    return elements


def render(bot, op, page=None, *, details=False):
    plan, result = _body(op['plan']), _body(op['result'])
    state, identifier = op['state'], op['id']
    if details:
        members = _members(plan, selected_only=True)
        pages = max(1, math.ceil(len(members) / PAGE_SIZE))
        page = min(max(0, page or 0), pages - 1)
        elements = [_file_list(members[page * PAGE_SIZE:(page + 1) * PAGE_SIZE])]
        buttons = []
        if page:
            buttons.append(button(bot, '上一页', 'details', id=identifier, page=page - 1))
        if page + 1 < pages:
            buttons.append(button(bot, '下一页', 'details', id=identifier, page=page + 1))
        buttons.append(button(bot, '返回本次任务', 'result', id=identifier))
        return card(f'归档文件清单 · {page + 1}/{pages}', elements + [bot.buttons(*buttons)], shared=True)
    if not op['cancelled'] and (state == 'choosing' or state == 'ready' and page is not None):
        return _chooser(bot, op, plan, page or 0)
    if state in ('queued', 'scanning'):
        return card('正在停止素材扫描' if op['cancelled'] else '正在读取相机素材',
                    [paragraph('正在结束扫描。' if op['cancelled'] else
                               '正在核对相机身份和读取文件清单，完成后在本卡片选择素材。'),
                     note(PRESERVATION),
                     bot.buttons(button(bot, '停止本次任务', 'cancel', id=identifier))], shared=True)
    if state == 'ready' and not op['cancelled']:
        elements = _summary(result)
        members = _members(plan, selected_only=True)
        elements += [markdown('**归档文件**'), _file_list(members[:PAGE_SIZE])]
        if len(members) > PAGE_SIZE:
            elements.append(note(f'已展示前 {PAGE_SIZE} 个文件，完整清单共 {len(members)} 个。'))
        elements.append(note('导入与上传并行，首个文件导入完成后即开始上传。'))
        elements.append(note(PRESERVATION))
        confirm = button(bot, '确认导入并上传', 'confirm', id=identifier,
                         choice_revision=op['choice_revision'])
        confirm['type'] = 'primary'
        choices = ([button(bot, '查看完整清单', 'details', id=identifier, page=0)]
                   if len(members) > PAGE_SIZE else [])
        choices.append(button(bot, '修改选择', 'edit', id=identifier, page=0))
        return card('选择导入预览', elements + [
            bot.buttons(*choices),
            bot.buttons(confirm, button(bot, '取消', 'cancel', id=identifier))], shared=True)
    titles = {'queued_transfer': '选择导入已排队', 'running': '选择导入进行中',
              'waiting': '选择导入等待恢复', 'paused': '选择导入已停止', 'done': '选择导入已完成'}
    title = titles.get(state, '选择导入')
    if state in ACTIVE and op['cancelled']:
        title = '正在停止选择导入'
    elements = _summary(result) if 'total_files' in result else []
    if 'total_files' in result:
        elements += _progress(result, state)
    error = result.get('error') or result.get('reason')
    error = {'pending_files': '部分选中素材尚未完成，连接恢复后自动重试。',
             'cache_cleanup': '归档已完成，正在重试清理本地缓存。',
             'archive_verification': '已有归档需重新核验，暂不计为完成。'}.get(error, error)
    if error:
        elements.append(paragraph(error))
    verification_pending = _count(result, 'archive_verification_pending')
    if verification_pending:
        elements.append(markdown(f'**归档待核验：{verification_pending} 个文件**'))
        if result.get('archive_errors'):
            first = result['archive_errors'][0]
            elements.append(paragraph(str(first.get('file') or '未确认的归档文件') +
                                      '\n' + str(first.get('reason') or '等待存储连接恢复后复查。')))
    complete = bool(result.get('total_files') and not verification_pending and
                    _count(result, 'uploaded_files') == _count(result, 'total_files'))
    if state == 'done' and not complete:
        title = '本次选择导入已结束'
        if not error:
            elements.append(paragraph('本次未执行传输。' if not result.get('imported_files') and
                                      not result.get('uploaded_files') else '未完成的任务和完整缓存保留。'))
    cleanup = result.get('cache_cleanup_pending') or result.get('cleanup_pending') or 0
    if cleanup:
        elements.append(paragraph(f'已归档，缓存待清理 {int(cleanup)} 个。'))
    elements.append(note(PRESERVATION))
    buttons = []
    if state in ACTIVE:
        buttons.append(button(bot, '停止本次任务', 'cancel', id=identifier))
    if state == 'paused' and plan.get('confirmed'):
        buttons.append(button(bot, '继续本次任务', 'resume', id=identifier))
    if plan.get('selected'):
        buttons.append(button(bot, '查看选中清单', 'details', id=identifier, page=0))
    if state == 'done' or state == 'paused' and not plan.get('confirmed'):
        buttons.append(button(bot, '重新选择素材', 'browse'))
    if buttons:
        elements.append(bot.buttons(*buttons))
    color = 'orange' if error or cleanup or verification_pending or state in ('paused', 'waiting') else 'green' if state == 'done' and complete else 'blue'
    return card(title, elements, color, shared=True)


def process_job(bot, job, payload):
    from .selected_imports import enqueue, choose, review, confirm, cancel, get, resume, progress
    action, waiting = payload['selected_import'], False
    store = Store(bot.directory)
    try:
        kind = action['action']
        identifier = payload.get('selected_request')
        if not identifier:
            if kind == 'browse':
                op = enqueue(store, bot.settings, job['id'], bot.scope)
            elif kind == 'choose':
                op = choose(store, bot.settings, action['id'], bot.scope, payload['selected_indices'],
                            action['page'], action['choice_revision'])
                if action['review']:
                    op = review(store, bot.settings, action['id'], bot.scope)
            elif kind == 'confirm':
                op = confirm(store, bot.settings, action['id'], bot.scope, action['choice_revision'])
            elif kind == 'resume':
                op = resume(store, bot.settings, action['id'], bot.scope)
            else:
                op = get(store.db, action['id'], bot.scope)
                if not op:
                    raise ConfigError('本次选择已过期，请重新打开「选择素材」')
                if kind in ('cancel', 'drain'):
                    with store.db:
                        cancel(store.db, op['id'], bot.scope, drain=kind == 'drain')
                    op = get(store.db, op['id'], bot.scope)
            identifier = op['id']
            payload['selected_request'] = identifier
            with bot.db() as db:
                db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
        op = get(store.db, identifier, bot.scope)
        if not op:
            raise ConfigError('本次选择已过期，请重新打开「选择素材」')
        waiting = op['state'] in ACTIVE
        if _body(op['plan']).get('confirmed'):
            op = progress(store, op)
        observation = [op['state'], op['updated'], op['cancelled'], op['choice_revision'], op['result']]
        if job['state'] == 'waiting' and payload.get('selected_observation') == observation:
            with bot.db() as db:
                db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 5, job['id']))
            return False
        page = action.get('page', 0) if kind in ('details', 'edit') else (
            action['next_page'] if kind == 'choose' and not action['review'] else None)
        response = render(bot, op, page, details=kind == 'details')
        payload['selected_observation'] = observation
    except ConfigError as exc:
        response = card('选择导入未执行', [paragraph(str(exc))], 'orange', shared=True)
    finally:
        store.close()
    delivered = bot.reply(job, payload, response, waiting)
    with bot.db() as db:
        db.execute('UPDATE bot_jobs SET response=?,payload=? WHERE id=?',
                   (json.dumps(response), json.dumps(payload), job['id']))
        if waiting:
            db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 5, job['id']))
    return delivered
