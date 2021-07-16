from flask import Flask, redirect, url_for, render_template,request, send_file
from app import getQuotation
import os
from flask_bootstrap import Bootstrap
from flask_datepicker import datepicker

app = Flask(__name__)
Bootstrap(app)
datepicker(app)

@app.route("/", methods=["POST","GET"])
def login():
    return render_template("form.html")


@app.route("/generate", methods=["POST","GET"])
def generate():
    if request.method == 'POST':
        filename = getQuotation(request.form)
        return render_template("gen.html", filename=filename)

@app.route('/download/<filename>')
def downloadFile (filename):
    path = f'{filename}'
    return send_file(path, as_attachment=True)

if __name__ =="__main__":
    app.run(debug=True)