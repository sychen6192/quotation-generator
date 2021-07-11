from flask import Flask, redirect, url_for, render_template,request
from flask_bootstrap import Bootstrap
from flask_datepicker import datepicker

app = Flask(__name__)
Bootstrap(app)
datepicker(app)

@app.route("/", methods=["POST","GET"])
def hello():
    return render_template("hello.html")


@app.route("/quotation", methods=["POST","GET"])
def login():
    return render_template("form.html")


@app.route("/generate", methods=["POST","GET"])
def generate():
    if request.method == 'POST':
        return 'Hello ' + request.form.get('name')


if __name__ =="__main__":
    app.run(debug=True)