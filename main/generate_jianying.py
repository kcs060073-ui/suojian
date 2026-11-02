import argparse
from suo_jian.jianying.editable import generate_editable_draft

parser = argparse.ArgumentParser(description="从溯剪 Blueprint 生成可编辑剪映草稿")
parser.add_argument("blueprint")
parser.add_argument("video")
parser.add_argument("draft_root")
parser.add_argument("--name", default="溯剪-2-可编辑草稿")
parser.add_argument("--user-data")
parser.add_argument("--jianying-install")
parser.add_argument("--draft-blueprint", help="经用户确认的 draft_blueprint.json；不传则保留参考字幕")
parser.add_argument("--approved-by-user", action="store_true", help="确认用户已明确许可生成剪映草稿")
parser.add_argument("--replace-existing", action="store_true", help="明确允许覆盖同名剪映草稿")
args = parser.parse_args()
if not args.approved_by_user:
    parser.error("尚未获得用户许可。请先在工作台查看分析结果；明确许可后再传入 --approved-by-user。")
print(generate_editable_draft(args.blueprint, args.video, args.draft_root, args.name, args.user_data, args.jianying_install, args.draft_blueprint, args.replace_existing))
