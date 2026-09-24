from django.contrib import admin
from django.urls import include, path

from core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", core_views.home, name="home"),
    path("sign-in/", core_views.sign_in, name="login"),
    path("sign-out/", core_views.sign_out, name="logout"),
    path("account/", core_views.choose_tenant, name="choose_tenant"),
    path("password/", core_views.change_password, name="change_password"),
    path("attendance/", include("attendance.urls")),
]
