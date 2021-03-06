from django.conf.urls import url

from . import views

urlpatterns = [

    # note: in registering the badgr app at SurfConext, the callback url is explicitely registered on 'openid'
    url('^surfconext_ala/login/callback/$', views.callback, name='ala_callback'),
    url('^surfconext_ala/login/$', views.login, name="surfconext_ala_login"),
    url('^surfconext_ala/login/terms_accepted/(?P<state>[^/]+)/(?P<access_token>[^/]+)', views.after_terms_agreement, name="ala_terms_accepted_callback")
]
