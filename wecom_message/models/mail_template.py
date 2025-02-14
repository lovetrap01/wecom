# -*- coding: utf-8 -*-

import base64
import logging
import html2text
import re

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MailTemplate(models.Model):
    "Template for sending WeCom message"

    _inherit = "mail.template"
    _description = "WeCom Message Templates"
    _order = "name"

    # recipients
    message_to_user = fields.Char(string="To Users", help="Message recipients (users)",)
    message_to_party = fields.Char(
        string="To Departments", help="Message recipients (departments)",
    )
    message_to_tag = fields.Char(string="To Tags", help="Message recipients (tags)",)

    # content
    media_id = fields.Many2one(
        string="Media file id",
        comodel_name="wecom.material",
        help="Media file ID, which can be obtained by calling the upload temporary material interface",
    )
    body_json = fields.Text("Json Body", translate=True, default={})
    body_markdown = fields.Text("Markdown Body", translate=True,)

    use_templates = fields.Boolean("Is template message", default=False)
    templates_id = fields.Many2one("wecom.message.template", string="Message template")

    msgtype = fields.Selection(
        [
            ("text", "Text message"),
            ("image", "Picture message"),
            ("voice", "Voice messages"),
            ("video", "Video message"),
            ("file", "File message"),
            ("textcard", "Text card message"),
            ("news", "Graphic message"),
            ("mpnews", "Graphic message(mpnews)"),
            ("markdown", "Markdown message"),
            ("miniprogram", "Mini Program Notification Message"),
            ("taskcard", "Task card message"),
            ("template_card", "Template card message"),
        ],
        string="Message type",
        required=True,
        default="text",
    )

    # options
    safe = fields.Selection(
        [
            ("0", "Shareable"),
            ("1", "Cannot share and content shows watermark"),
            ("2", "Only share within the company "),
        ],
        string="Secret message",
        required=True,
        default="1",
        help="Indicates whether it is a confidential message, 0 indicates that it can be shared externally, 1 indicates that it cannot be shared and the content displays watermark, 2 indicates that it can only be shared within the enterprise, and the default is 0; Note that only messages of mpnews type support the safe value of 2, and other message types do not",
    )

    enable_id_trans = fields.Boolean(
        string="Turn on id translation",
        help="Indicates whether to enable ID translation, 0 indicates no, 1 indicates yes, and 0 is the default",
        default=False,
    )
    enable_duplicate_check = fields.Boolean(
        string="Turn on duplicate message checking",
        help="Indicates whether to enable duplicate message checking. 0 indicates no, 1 indicates yes. The default is 0",
        default=False,
    )
    duplicate_check_interval = fields.Integer(
        string="Time interval for repeated message checking",
        help="Indicates whether the message check is repeated. The default is 1800s and the maximum is no more than 4 hours",
        default="1800",
    )

    # ------------------------------------------------------------
    # 邮件/电子邮件值的生成
    # MESSAGE/EMAIL VALUES GENERATION
    # ------------------------------------------------------------
    def generate_wecom_message_recipients(self, results, res_ids):
        """Generates the recipients of the template. Default values can ben generated
        instead of the template values if requested by template or context.
        Emails (email_to, email_cc) can be transformed into partners if requested
        in the context."""
        self.ensure_one()

        if self.use_default_to or self._context.get("tpl_force_default_to"):
            records = self.env[self.model].browse(res_ids).sudo()
            default_recipients = records._message_get_default_recipients()
            for res_id, recipients in default_recipients.items():
                results[res_id].pop("partner_to", None)
                results[res_id].update(recipients)

        records_company = None
        if (
            self._context.get("tpl_partners_only")
            and self.model
            and results
            and "company_id" in self.env[self.model]._fields
        ):
            records = self.env[self.model].browse(results.keys()).read(["company_id"])
            records_company = {
                rec["id"]: (rec["company_id"][0] if rec["company_id"] else None)
                for rec in records
            }

        for res_id, values in results.items():
            partner_ids = values.get("partner_ids", list())
            if self._context.get("tpl_partners_only"):
                mails = (
                    tools.email_split(values.pop("message_to_user", ""))
                    + tools.email_split(values.pop("message_to_party", ""))
                    + tools.email_split(values.pop("message_to_tag", ""))
                )
                Partner = self.env["res.partner"]
                if records_company:
                    Partner = Partner.with_context(
                        default_company_id=records_company[res_id]
                    )
                for mail in mails:
                    partner = Partner.find_or_create(mail)
                    partner_ids.append(partner.id)
            partner_to = values.pop("partner_to", "")
            if partner_to:
                # placeholders could generate '', 3, 2 due to some empty field values
                tpl_partner_ids = [int(pid) for pid in partner_to.split(",") if pid]
                partner_ids += (
                    self.env["res.partner"].sudo().browse(tpl_partner_ids).exists().ids
                )
            results[res_id]["partner_ids"] = partner_ids
        return results

    def generate_wecom_message(self, res_ids, fields):
        """
        根据res_ids给定的记录，从给定给定模型的模板生成电子邮件。

        :param res_id: 用于呈现模板的记录的ID（模型来自模板定义）
        :returns: 包含所有用于创建新 mail.mail 条目的所有相关字段的字典，带有一个额外的键“附件”，格式为[（report_name，data）]，其中数据是base64编码的。
        """
        self.ensure_one()
        multi_mode = True
        if isinstance(res_ids, int):
            res_ids = [res_ids]
            multi_mode = False

        results = dict()
        for lang, (template, template_res_ids) in self._classify_per_lang(
            res_ids
        ).items():
            for field in fields:
                template = template.with_context(safe=(field == "subject"))
                if template.msgtype == "mpnews":
                    generated_field_values = template._render_field(
                        field, template_res_ids, post_process=(field == "body_html")
                    )
                elif template.msgtype == "markdown":
                    generated_field_values = template._render_field(
                        field, template_res_ids, post_process=(field == "body_markdown")
                    )
                else:
                    generated_field_values = template._render_field(
                        field, template_res_ids, post_process=(field == "body_json"),
                    )
                for res_id, field_value in generated_field_values.items():
                    results.setdefault(res_id, dict())[field] = field_value

            # 计算收件人
            if any(
                field in fields
                for field in [
                    "partner_to",
                    "message_to_user",
                    "message_to_party",
                    "message_to_tag",
                ]
            ):
                results = template.generate_wecom_message_recipients(
                    results, template_res_ids
                )

            # 更新所有res_id的值
            for res_id in template_res_ids:
                values = results[res_id]
                if values.get("body_html"):
                    values["body_html"] = tools.html_sanitize(values["body_html"])
                if values.get("body_json"):
                    values["body_json"] = tools.html2plaintext(
                        values["body_json"]
                    )  # 将HTML转换为纯文本
                if values.get("body_markdown"):
                    values["body_markdown"] = tools.html2plaintext(
                        values["body_markdown"]
                    )  # 将HTML转换为纯文本
                # 技术设置
                values.update(
                    mail_server_id=template.mail_server_id.id or False,
                    auto_delete=template.auto_delete,
                    model=template.model,
                    res_id=res_id or False,
                    attachment_ids=[attach.id for attach in template.attachment_ids],
                )

            # Add report in attachments: generate once for all template_res_ids
            if template.report_template:
                for res_id in template_res_ids:
                    attachments = []
                    report_name = template._render_field("report_name", [res_id])[
                        res_id
                    ]
                    report = template.report_template
                    report_service = report.report_name

                    if report.report_type in ["qweb-html", "qweb-pdf"]:
                        result, format = report._render_qweb_pdf([res_id])
                    else:
                        res = report._render([res_id])
                        if not res:
                            raise UserError(
                                _(
                                    "Unsupported report type %s found.",
                                    report.report_type,
                                )
                            )
                        result, format = res

                    # TODO in trunk, change return format to binary to match message_post expected format
                    result = base64.b64encode(result)
                    if not report_name:
                        report_name = "report." + report_service
                    ext = "." + format
                    if not report_name.endswith(ext):
                        report_name += ext
                    attachments.append((report_name, result))
                    results[res_id]["attachments"] = attachments

        return multi_mode and results or results[res_ids[0]]

    # ------------------------------------------------------------
    # 发送邮件 和 企微消息
    # ------------------------------------------------------------
    def send_message(
        self,
        res_id,
        force_send=False,
        raise_exception=False,
        email_values=None,
        notif_layout=False,
    ):
        """
        生成一个新的mail.mail。模板呈现在模板的res_id和模型给出的记录上。

        :param int res_id: 用于呈现模板的记录的id
        :param bool force_send: 立即发送电子邮件；否则使用邮件队列（推荐）；
        :param dict email_values: 使用这些值更新生成的邮件，以进一步自定义邮件；
        :param str notif_layout: 用于封装生成的电子邮件的可选通知布局；
        :returns: 已创建的mail.mail的id
        """

        # 仅当访问相关文档时才授予发送邮件的权限
        self.ensure_one()
        self._send_check_access([res_id])

        Attachment = self.env[
            "ir.attachment"
        ]  # TDE FIXME: should remove default_type from context

        # create a mail_mail based on values, without attachments
        values = self.generate_wecom_message(
            res_id,
            [
                "subject",
                "body_html",
                "email_from",
                "partner_to",
                "scheduled_date",
                # 企微消息 start
                "msgtype",
                "message_to_user",
                "message_to_party",
                "message_to_tag",
                "media_id",
                "body_json",
                "body_markdown",
                "safe",
                "enable_id_trans",
                "enable_duplicate_check",
                "duplicate_check_interval",
                # 企微消息 end
            ],
        )

        # 获取公司
        record = self.env[self.model].browse(res_id)
        company = record.company_id

        values["recipient_ids"] = [
            (4, pid) for pid in values.get("partner_ids", list())
        ]
        values["attachment_ids"] = [
            (4, aid) for aid in values.get("attachment_ids", list())
        ]
        values.update(email_values or {})
        attachment_ids = values.pop("attachment_ids", [])
        attachments = values.pop("attachments", [])

        # 添加针对来自的无效电子邮件的保护
        if "email_from" in values and not values.get("email_from"):
            values.pop("email_from")
        model = self.env["ir.model"]._get(record._name)
        if values["msgtype"] == "mpnews":
            # 封装 body_html
            del values["body_markdown"]
            del values["body_json"]
            if notif_layout and values["body_html"]:
                try:
                    template = self.env.ref(notif_layout, raise_if_not_found=True)
                except ValueError:
                    _logger.warning(
                        "QWeb template %s not found when sending template %s. Sending without layouting."
                        % (notif_layout, self.name)
                    )
                else:
                    if self.lang:
                        lang = self._render_lang([res_id])[res_id]
                        template = template.with_context(lang=lang)
                        model = model.with_context(lang=lang)

                    template_ctx = {
                        "message": self.env["mail.message"]
                        .sudo()
                        .new(
                            dict(
                                body_html=values["body_html"],
                                record_name=record.display_name,
                            )
                        ),
                        "model_description": model.display_name,
                        "company": "company_id" in record
                        and record["company_id"]
                        or self.env.company,
                        "record": record,
                    }
                    body = template._render(
                        template_ctx, engine="ir.qweb", minimal_qcontext=True
                    )
                    values["body_html"] = self.env[
                        "mail.render.mixin"
                    ]._replace_local_links(body)
        elif values["msgtype"] == "markdown":
            # 封装 body_markdown
            del values["body_html"]
            del values["body_json"]
            if notif_layout and values["body_markdown"]:
                try:
                    template = self.env.ref(notif_layout, raise_if_not_found=True)
                except ValueError:
                    _logger.warning(
                        "QWeb template %s not found when sending template %s. Sending without layouting."
                        % (notif_layout, self.name)
                    )
                else:

                    if self.lang:
                        lang = self._render_lang([res_id])[res_id]
                        template = template.with_context(lang=lang)
                        model = model.with_context(lang=lang)

                    template_ctx = {
                        "message": self.env["mail.message"]
                        .sudo()
                        .new(
                            dict(
                                markdown_body=values["body_markdown"],
                                record_name=record.display_name,
                            )
                        ),
                        "model_description": model.display_name,
                        "company": "company_id" in record
                        and record["company_id"]
                        or self.env.company,
                        "record": record,
                    }
                    body = template._render(
                        template_ctx, engine="ir.qweb", minimal_qcontext=True
                    )
                    #  tools.html2plaintext()
                    values["body_markdown"] = self.env[
                        "mail.render.mixin"
                    ]._replace_local_links(body)
        else:
            # 封装 body_json
            del values["body_html"]
            del values["body_markdown"]

            if notif_layout and values["body_json"]:
                try:
                    template = self.env.ref(notif_layout, raise_if_not_found=True)
                except ValueError:
                    _logger.warning(
                        "QWeb template %s not found when sending template %s. Sending without layouting."
                        % (notif_layout, self.name)
                    )
                else:
                    if self.lang:
                        lang = self._render_lang([res_id])[res_id]
                        template = template.with_context(lang=lang)
                        model = model.with_context(lang=lang)

                    template_ctx = {
                        "message": self.env["mail.message"]
                        .sudo()
                        .new(
                            dict(
                                body_json=values["body_json"],
                                record_name=record.display_name,
                            )
                        ),
                        "model_description": model.display_name,
                        "company": "company_id" in record
                        and record["company_id"]
                        or self.env.company,
                        "record": record,
                    }
                    body_json = template._render(
                        template_ctx, engine="ir.qweb", minimal_qcontext=True
                    )

                    values["body_json"] = self.env[
                        "mail.render.mixin"
                    ]._replace_local_links(body_json)

                    print(values["body_json"])

                    # .compile("(?<=\>).*?(?=\<)")

        if values.get("media_id"):
            values["media_id"] = self.media_id.id  # 指定对应的素材id
        values["use_templates"] = True  # 指定使用模板
        values["templates_id"] = self.id  # 指定对应的模板id
        mail = self.env["mail.mail"].sudo().create(values)

        # 管理附件
        for attachment in attachments:
            attachment_data = {
                "name": attachment[0],
                "datas": attachment[1],
                "type": "binary",
                "res_model": "mail.message",
                "res_id": mail.mail_message_id.id,
            }
            attachment_ids.append((4, Attachment.create(attachment_data).id))
        if attachment_ids:
            mail.write({"attachment_ids": attachment_ids})

        # 指定对应的素材id

        # 标识是企微消息
        is_wecom_message = False
        if "message_to_user" in values and values["message_to_user"]:
            is_wecom_message = True
        if "message_to_party" in values and values["message_to_party"]:
            is_wecom_message = True
        if "message_to_tag" in values and values["message_to_tag"]:
            is_wecom_message = True

        mail.write({"is_wecom_message": is_wecom_message})

        if force_send:
            mail.send_wecom_mail_message(
                raise_exception=raise_exception, company=company
            )
        return mail.id  # TDE CLEANME: return mail + api.returns ?
