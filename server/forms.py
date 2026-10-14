# -*- coding: utf-8 -*-
"""Flask-WTF 表单 (CSRF 由 Flask-WTF 全站覆盖)"""
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import (DataRequired, EqualTo, Length, Optional, Regexp,
                                ValidationError)

USERNAME_RE = r"^[A-Za-z0-9_]{3,32}$"


class LoginForm(FlaskForm):
    username = StringField("账号", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("密码", validators=[DataRequired()])
    remember = BooleanField("记住我")
    submit = SubmitField("登录")


class RegisterForm(FlaskForm):
    username = StringField("账号", validators=[
        DataRequired(), Regexp(USERNAME_RE, message="3~32位, 仅字母/数字/下划线")])
    nickname = StringField("昵称", validators=[Optional(), Length(max=32)])
    password = PasswordField("密码", validators=[
        DataRequired(), Length(min=8, max=64, message="至少8位")])
    confirm = PasswordField("确认密码", validators=[
        DataRequired(), EqualTo("password", message="两次密码不一致")])
    submit = SubmitField("注册")

    def validate_username(self, field):
        from server.models import User
        if User.query.filter_by(username=field.data).first():
            raise ValidationError("该账号已被使用")


class ThemeForm(FlaskForm):
    theme = SelectField("界面主题", choices=[("light", "白天"), ("dark", "黑夜")])
    submit = SubmitField("保存")


class NicknameForm(FlaskForm):
    nickname = StringField("新昵称", validators=[DataRequired(), Length(max=32)])
    submit = SubmitField("保存昵称")


class PasswordChangeForm(FlaskForm):
    old = PasswordField("当前密码", validators=[DataRequired()])
    new = PasswordField("新密码", validators=[
        DataRequired(), Length(min=8, max=64, message="至少8位")])
    confirm = PasswordField("确认新密码", validators=[
        DataRequired(), EqualTo("new", message="两次密码不一致")])
    submit = SubmitField("修改密码")


class UserAddForm(FlaskForm):
    username = StringField("账号", validators=[
        DataRequired(), Regexp(USERNAME_RE, message="3~32位, 仅字母/数字/下划线")])
    nickname = StringField("昵称", validators=[Optional(), Length(max=32)])
    password = PasswordField("初始密码", validators=[
        DataRequired(), Length(min=8, max=64, message="至少8位")])
    level = SelectField("权限等级", choices=[], coerce=int)
    submit = SubmitField("添加")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from server.auth import MANAGER_LEVELS
        self.level.choices = MANAGER_LEVELS
        self.level.coerce = int

    def validate_username(self, field):
        from server.models import User
        if User.query.filter_by(username=field.data).first():
            raise ValidationError("该账号已存在")


class ResetPwForm(FlaskForm):
    new = PasswordField("新密码", validators=[
        DataRequired(), Length(min=8, max=64, message="至少8位")])
    confirm = PasswordField("确认新密码", validators=[
        DataRequired(), EqualTo("new", message="两次密码不一致")])
    submit = SubmitField("重置")
