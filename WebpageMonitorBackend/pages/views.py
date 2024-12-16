from django.shortcuts import render

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

import json

# Create your views here.

def homePageView(request):
    return HttpResponse("Hello, World")

@csrf_exempt
def monitor(request):
    #csrf_token = request.POST.get('csrfmiddlewaretoken')
    if request.method == 'POST':
        url = json.loads(request.body.decode('utf-8'))['webpageURL']
        print(url)
        return HttpResponse("url received")
    else:
        return HttpResponse("GET request not supported")
