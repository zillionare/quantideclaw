import tkinter as tk

def main():
    root = tk.Tk()
    root.title("Hello World Test")
    root.geometry("400x300")

    label = tk.Label(root, text="Hello, World!", font=("Arial", 24))
    label.pack(expand=True)

    btn = tk.Button(root, text="Close", command=root.destroy)
    btn.pack(pady=20)

    print("Window should now be visible...")
    root.mainloop()
    print("Window closed.")

if __name__ == "__main__":
    main()